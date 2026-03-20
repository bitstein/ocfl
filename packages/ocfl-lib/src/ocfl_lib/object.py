"""OCFL object read and write operations."""

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, BinaryIO, Literal

from ocfl_lib.digest import DigestAlgorithm
from ocfl_lib.digest_map import DigestMap, to_path_map
from ocfl_lib.fs import utils as fs_utils
from ocfl_lib.inventory import StoredInventory, UserInfo, VersionInfo
from ocfl_lib.inventory_builder import InventoryBuilder
from ocfl_lib.spec import INVENTORY_FILENAME, Namaste, Spec, VNum
from ocfl_lib.update_plan import (
    ContentSource,
    UpdatePlan,
    execute_plan,
    prepare_steps,
    revert_plan,
)

if TYPE_CHECKING:
    from ocfl_lib.fs.protocols import FS, WriteFS
    from ocfl_lib.stage import Stage


@dataclass(frozen=True, slots=True)
class DiffEntry:
    """A single change between two version states."""

    path: str
    action: Literal["added", "deleted", "modified", "renamed"]
    digest_before: str | None = None
    digest_after: str | None = None
    renamed_from: str | None = None


class Object:
    """Read-only interface to an existing OCFL object."""

    def __init__(self, fsys: FS, path: str) -> None:
        self._fsys = fsys
        self._path = path
        self._stored_inv: StoredInventory | None = None

    @property
    def exists(self) -> bool:
        """True if an OCFL object declaration exists at this path."""
        try:
            entries = list(self._fsys.read_dir(self._path))
        except OSError:
            return False
        for e in entries:
            n = Namaste.parse(e.name)
            if n is not None and n.kind == "object":
                return True
        return False

    @property
    def inventory(self) -> StoredInventory:
        """Root inventory, lazily loaded and cached. Validates sidecar on load."""
        if self._stored_inv is None:
            self._stored_inv = self._load_inventory(self._path)
        return self._stored_inv

    @property
    def id(self) -> str:
        return self.inventory.inventory.id

    @property
    def spec(self) -> Spec:
        return Spec.from_inventory_type(self.inventory.inventory.type)

    @property
    def head(self) -> VNum:
        return VNum.parse(self.inventory.inventory.head)

    def versions(self) -> list[VNum]:
        """Sorted list of all version numbers."""
        vnums = [VNum.parse(k) for k in self.inventory.inventory.versions]
        vnums.sort()
        return vnums

    def version_state(self, v: VNum) -> DigestMap:
        """State (digest → [logical_paths]) for a specific version."""
        return self.version_info(v).state

    def version_info(self, v: VNum) -> VersionInfo:
        """Metadata for a specific version. Raises KeyError if not found."""
        vkey = str(v)
        versions = self.inventory.inventory.versions
        if vkey not in versions:
            raise KeyError(f"version {vkey} not found")
        return versions[vkey]

    def version_stage(self, v: VNum) -> Stage:
        """Create a Stage from an existing version's state.

        The stage's ContentSource is backed by this object's manifest.
        Raises KeyError if version not found.
        """
        from ocfl_lib.stage import Stage

        state = self.version_state(v)
        inv = self.inventory.inventory
        alg = DigestAlgorithm.from_ocfl_name(inv.digest_algorithm)
        content_source = _ObjectContentSource(self._fsys, self._path, inv.manifest)
        return Stage(
            state=state,
            digest_algorithm=alg,
            content_source=content_source,
        )

    def content_path(self, digest: str) -> str | None:
        """Manifest lookup: digest → first content path, or None."""
        paths = self.inventory.inventory.manifest.get(digest)
        if paths:
            return paths[0]
        return None

    def open_logical_path(self, v: VNum, logical_path: str) -> BinaryIO:
        """Open a file by logical path in a version.

        Raises KeyError if logical_path not in version state.
        Raises FileNotFoundError if content file missing on disk.
        """
        state = self.version_state(v)
        path_map = to_path_map(state)  # logical_path → digest
        if logical_path not in path_map:
            raise KeyError(f"logical path {logical_path!r} not in version {v} state")
        digest = path_map[logical_path]
        cp = self.content_path(digest)
        if cp is None:
            raise FileNotFoundError(f"digest {digest[:16]}... not found in manifest")
        full_path = _join(self._path, cp)
        return self._fsys.open_file(full_path)

    def diff_versions(self, v1: VNum, v2: VNum) -> list[DiffEntry]:
        """Compute changes between two versions.

        Detects added, deleted, modified, and renamed files.
        Rename detection: pairs added/removed files sharing a digest.
        """
        pm1 = to_path_map(self.version_state(v1))  # path → digest
        pm2 = to_path_map(self.version_state(v2))

        all_paths = set(pm1) | set(pm2)
        added: dict[str, str] = {}  # path → digest
        removed: dict[str, str] = {}  # path → digest
        entries: list[DiffEntry] = []

        for path in sorted(all_paths):
            d1 = pm1.get(path)
            d2 = pm2.get(path)
            if d1 == d2:
                continue
            if d1 is None:
                assert d2 is not None
                added[path] = d2
            elif d2 is None:
                removed[path] = d1
            else:
                entries.append(
                    DiffEntry(
                        path=path,
                        action="modified",
                        digest_before=d1,
                        digest_after=d2,
                    )
                )

        # Rename detection: pair added/removed sharing a digest
        # Build digest → [paths] maps for added and removed
        added_by_digest: dict[str, list[str]] = {}
        for path, digest in added.items():
            added_by_digest.setdefault(digest, []).append(path)
        removed_by_digest: dict[str, list[str]] = {}
        for path, digest in removed.items():
            removed_by_digest.setdefault(digest, []).append(path)

        paired_added: set[str] = set()
        paired_removed: set[str] = set()

        for digest in added_by_digest:
            if digest not in removed_by_digest:
                continue
            a_paths = sorted(added_by_digest[digest])
            r_paths = sorted(removed_by_digest[digest])
            # Pair up: min(len(a), len(r)) renames, rest are pure adds/removes
            for a_path, r_path in zip(a_paths, r_paths, strict=False):
                entries.append(
                    DiffEntry(
                        path=a_path,
                        action="renamed",
                        digest_before=digest,
                        digest_after=digest,
                        renamed_from=r_path,
                    )
                )
                paired_added.add(a_path)
                paired_removed.add(r_path)

        # Remaining unpaired adds/removes
        for path in sorted(added):
            if path not in paired_added:
                entries.append(
                    DiffEntry(path=path, action="added", digest_after=added[path])
                )
        for path in sorted(removed):
            if path not in paired_removed:
                entries.append(
                    DiffEntry(path=path, action="deleted", digest_before=removed[path])
                )

        entries.sort(key=lambda e: e.path)
        return entries

    def version_inventory(self, v: VNum) -> StoredInventory | None:
        """Load inventory from a specific version directory, or None if absent."""
        vdir = _join(self._path, str(v))
        inv_path = _join(vdir, INVENTORY_FILENAME)
        if not fs_utils.exists(self._fsys, inv_path):
            return None
        return self._load_inventory(vdir)

    def update(
        self,
        fsys: WriteFS,
        stage: Stage,
        *,
        id: str | None = None,
        spec: Spec | None = None,
        message: str | None = None,
        user: UserInfo | None = None,
        created: str | None = None,
    ) -> None:
        """Create or update an OCFL object version.

        For new objects (not self.exists), id is required.
        Builds inventory, creates update plan, executes it.
        On error: reverts completed steps, then re-raises.
        Updates cached inventory on success.
        """
        plan = self.new_update_plan(
            stage,
            id=id,
            spec=spec,
            message=message,
            user=user,
            created=created,
        )
        prepare_steps(plan, fsys, self._path, stage.content_source)

        try:
            execute_plan(plan)
        except Exception:
            revert_plan(plan)
            raise

        self._stored_inv = plan.new_inv

    def new_update_plan(
        self,
        stage: Stage,
        *,
        id: str | None = None,
        spec: Spec | None = None,
        message: str | None = None,
        user: UserInfo | None = None,
        created: str | None = None,
    ) -> UpdatePlan:
        """Build an update plan without executing it.

        Does NOT embed ContentSource in the plan. Use apply_update_plan()
        to execute with a ContentSource.
        """
        old_stored = self.inventory if self.exists else None
        prev = old_stored.inventory if old_stored else None

        builder = InventoryBuilder(prev=prev)
        if id is not None:
            builder.id(id)
        if spec is not None:
            builder.spec(spec)
        if stage.fixity_source is not None:
            builder.fixity_source(stage.fixity_source)

        builder.add_version(
            stage.state,
            created=created,
            message=message,
            user=user,
        )
        new_inv = builder.build()

        alg = DigestAlgorithm.from_ocfl_name(new_inv.digest_algorithm)
        new_stored = StoredInventory.from_inventory(new_inv, alg)

        return UpdatePlan(
            steps=[],
            new_inv=new_stored,
            old_inv=old_stored,
        )

    def apply_update_plan(
        self,
        fsys: WriteFS,
        plan: UpdatePlan,
        content_source: ContentSource,
    ) -> None:
        """Execute a previously created update plan.

        Takes ContentSource separately (enables resume with different source).
        No auto-revert — caller handles errors and decides whether to revert.
        Updates cached inventory on success.
        """
        prepare_steps(plan, fsys, self._path, content_source)
        execute_plan(plan)
        self._stored_inv = plan.new_inv

    def _load_inventory(self, dir_path: str) -> StoredInventory:
        """Read inventory.json + sidecar, validate digest, return StoredInventory."""
        inv_path = _join(dir_path, INVENTORY_FILENAME)
        inv_bytes = fs_utils.read_all(self._fsys, inv_path)

        # Determine algorithm from inventory JSON
        inv_data = json.loads(inv_bytes)
        alg_name = inv_data.get("digestAlgorithm", "sha512")
        alg = DigestAlgorithm.from_ocfl_name(alg_name)

        # Validate sidecar digest
        sidecar_name = f"{INVENTORY_FILENAME}.{alg.ocfl_name}"
        sidecar_path = _join(dir_path, sidecar_name)
        sidecar_text = fs_utils.read_text(self._fsys, sidecar_path)
        expected_digest = sidecar_text.strip().split()[0]

        stored = StoredInventory.from_bytes(inv_bytes, alg)
        if stored.digest != expected_digest:
            raise ValueError(
                f"inventory sidecar digest mismatch in {sidecar_path}: "
                f"expected {expected_digest[:16]}..., got {stored.digest[:16]}..."
            )

        return stored


class _ObjectContentSource:
    """ContentSource backed by an object's manifest."""

    def __init__(self, fsys: FS, obj_path: str, manifest: dict[str, list[str]]):
        self._fsys = fsys
        self._obj_path = obj_path
        self._manifest = manifest

    def get_content(self, digest: str) -> tuple[FS, str]:
        paths = self._manifest.get(digest)
        if not paths:
            raise KeyError(f"digest {digest!r} not in manifest")
        full = _join(self._obj_path, paths[0])
        return self._fsys, full


def _join(base: str, name: str) -> str:
    """Join path components with '/' separator, handling empty base."""
    if not base:
        return name
    return f"{base}/{name}"
