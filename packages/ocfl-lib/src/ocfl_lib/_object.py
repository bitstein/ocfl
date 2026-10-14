import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, BinaryIO, Literal, Self

from ocfl_lib._digest import DigestAlgorithm
from ocfl_lib._digest_map import PathMap, from_path_map, to_path_map
from ocfl_lib._errors import (
    ContentNotFound,
    InventorySidecarMismatch,
    LogicalPathNotFound,
    VersionNotFound,
)
from ocfl_lib._inventory import StoredInventory, VersionInfo
from ocfl_lib._path import join_path
from ocfl_lib._sources import ContentSource
from ocfl_lib._spec import INVENTORY_FILENAME, Namaste, Spec, VNum
from ocfl_lib.fs import _utils as fs_utils

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ocfl_lib._version_state import VersionState
    from ocfl_lib.fs._protocols import FS


@dataclass(frozen=True, slots=True)
class DiffEntry:
    """A single change between two version states."""

    path: str
    action: Literal["added", "deleted", "modified", "renamed"]
    digest_before: str | None = None
    digest_after: str | None = None
    renamed_from: str | None = None


class Object:
    """Read-only interface to an OCFL object. The constructor does no I/O.

    The root inventory is read lazily and cached. Write new versions with
    `StorageRoot.commit`.
    """

    def __init__(self, fsys: FS, path: str) -> None:
        self._fsys = fsys
        self._path = path
        self._stored_inv: StoredInventory | None = None

    @classmethod
    def load(cls, fsys: FS, path: str) -> Self:
        """Object at `path` with its root inventory already read."""
        obj = cls(fsys, path)
        obj._stored_inv = obj._load_inventory(path)
        return obj

    @property
    def fs(self) -> FS:
        """Filesystem the object is read from."""
        return self._fsys

    @property
    def path(self) -> str:
        """Object root path within `fs`."""
        return self._path

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

    def versions(self) -> Sequence[VNum]:
        """Sorted list of all version numbers."""
        vnums = [VNum.parse(k) for k in self.inventory.inventory.versions]
        vnums.sort()
        return vnums

    def version_info(self, v: VNum) -> VersionInfo:
        """Metadata for a specific version. Raises VersionNotFound if absent."""
        vkey = str(v)
        versions = self.inventory.inventory.versions
        if vkey not in versions:
            raise VersionNotFound(vkey)
        return versions[vkey]

    def version_state(self, v: VNum) -> VersionState:
        """VersionState of an existing version.

        Its ContentSource is backed by this object's manifest.
        Raises VersionNotFound if the version is absent.
        """
        from ocfl_lib._version_state import VersionState

        state = self.version_info(v).state
        inv = self.inventory.inventory
        alg = DigestAlgorithm.from_ocfl_name(inv.digest_algorithm)
        content_source = _ObjectContentSource(self._fsys, self._path, inv.manifest)
        return VersionState(
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
        state = self.version_info(v).state
        path_map = to_path_map(state)
        if logical_path not in path_map:
            raise LogicalPathNotFound(
                f"logical path {logical_path!r} not in version {v} state"
            )
        digest = path_map[logical_path]
        cp = self.content_path(digest)
        if cp is None:
            raise ContentNotFound(f"digest {digest[:16]}... not found in manifest")
        full_path = join_path(self._path, cp)
        return self._fsys.open_file(full_path)

    def diff_versions(self, v1: VNum, v2: VNum) -> Sequence[DiffEntry]:
        """Compute changes between two versions.

        Detects added, deleted, modified, and renamed files.
        Rename detection: pairs added/removed files sharing a digest.
        """
        pm1 = to_path_map(self.version_info(v1).state)
        pm2 = to_path_map(self.version_info(v2).state)

        all_paths = set(pm1) | set(pm2)
        added: PathMap = {}
        removed: PathMap = {}
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

        renames = _pair_renames(added, removed)
        renamed_to = {e.path for e in renames}
        renamed_from = {e.renamed_from for e in renames}
        entries.extend(renames)
        entries.extend(
            DiffEntry(path=path, action="added", digest_after=digest)
            for path, digest in added.items()
            if path not in renamed_to
        )
        entries.extend(
            DiffEntry(path=path, action="deleted", digest_before=digest)
            for path, digest in removed.items()
            if path not in renamed_from
        )

        entries.sort(key=lambda e: e.path)
        return entries

    def version_inventory(self, v: VNum) -> StoredInventory | None:
        """Load inventory from a specific version directory, or None if absent."""
        vdir = join_path(self._path, str(v))
        inv_path = join_path(vdir, INVENTORY_FILENAME)
        if not self._fsys.exists(inv_path):
            return None
        return self._load_inventory(vdir)

    def _load_inventory(self, dir_path: str) -> StoredInventory:
        """Read inventory.json + sidecar, validate digest, return StoredInventory."""
        inv_path = join_path(dir_path, INVENTORY_FILENAME)
        inv_bytes = fs_utils.read_all(self._fsys, inv_path)

        inv_data = json.loads(inv_bytes)
        alg_name = inv_data.get("digestAlgorithm", "sha512")
        alg = DigestAlgorithm.from_ocfl_name(alg_name)

        sidecar_name = f"{INVENTORY_FILENAME}.{alg.ocfl_name}"
        sidecar_path = join_path(dir_path, sidecar_name)
        sidecar_text = fs_utils.read_text(self._fsys, sidecar_path)
        expected_digest = sidecar_text.strip().split()[0]

        stored = StoredInventory.from_bytes(inv_bytes, alg)
        if stored.digest != expected_digest:
            raise InventorySidecarMismatch(
                path=sidecar_path,
                expected=expected_digest,
                actual=stored.digest,
            )

        return stored


class _ObjectContentSource(ContentSource):
    """ContentSource backed by an object's manifest."""

    def __init__(self, fsys: FS, obj_path: str, manifest: dict[str, list[str]]):
        self._fsys = fsys
        self._obj_path = obj_path
        self._manifest = manifest

    def get_content(self, digest: str) -> tuple[FS, str]:
        paths = self._manifest.get(digest)
        if not paths:
            raise ContentNotFound(f"digest {digest!r} not in manifest")
        full = join_path(self._obj_path, paths[0])
        return self._fsys, full


def _pair_renames(added: PathMap, removed: PathMap) -> list[DiffEntry]:
    """Pair added with removed paths of one digest, in sorted path order.

    Paths a digest has more of on one side than the other stay unpaired.
    """
    removed_by_digest = from_path_map(removed)
    return [
        DiffEntry(
            path=to,
            action="renamed",
            digest_before=digest,
            digest_after=digest,
            renamed_from=frm,
        )
        for digest, to_paths in from_path_map(added).items()
        for to, frm in zip(
            sorted(to_paths), sorted(removed_by_digest.get(digest, [])), strict=False
        )
    ]
