"""OCFL object read operations."""

import json
from typing import TYPE_CHECKING, BinaryIO

from ocfl_lib.digest import DigestAlgorithm
from ocfl_lib.digest_map import DigestMap, to_path_map
from ocfl_lib.fs import utils as fs_utils
from ocfl_lib.inventory import StoredInventory, VersionInfo
from ocfl_lib.spec import INVENTORY_FILENAME, Namaste, Spec, VNum

if TYPE_CHECKING:
    from ocfl_lib.fs.protocols import FS


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

    def version_inventory(self, v: VNum) -> StoredInventory | None:
        """Load inventory from a specific version directory, or None if absent."""
        vdir = _join(self._path, str(v))
        inv_path = _join(vdir, INVENTORY_FILENAME)
        if not fs_utils.exists(self._fsys, inv_path):
            return None
        return self._load_inventory(vdir)

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


def _join(base: str, name: str) -> str:
    """Join path components with '/' separator, handling empty base."""
    if not base:
        return name
    return f"{base}/{name}"
