"""Persistent staging store for OCFL CLI."""

import hashlib
import json
import shutil
from typing import TYPE_CHECKING

import msgspec

from ocfl_lib.inventory import Inventory

if TYPE_CHECKING:
    from pathlib import Path


class StagingStore:
    """Manages staging directories for in-progress OCFL object versions.

    Each (root_path, object_id) pair gets a staging directory containing:
    - inventory.json: full staged inventory (all versions + new head)
    - content.json: digest → {path, size, mtime} for new content files
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def staging_path(self, root_path: str, object_id: str) -> Path:
        """Deterministic staging directory for a (root, object) pair."""
        key = f"{root_path}\0{object_id}".encode()
        return self._root / hashlib.sha256(key).hexdigest()

    def exists(self, root_path: str, object_id: str) -> bool:
        return self.staging_path(root_path, object_id).is_dir()

    def create(self, root_path: str, object_id: str, inventory: Inventory) -> None:
        """Create staging directory with inventory and empty content map."""
        d = self.staging_path(root_path, object_id)
        d.mkdir(parents=True, exist_ok=True)
        self._write_inventory(d, inventory)
        self._write_content_map(d, {})

    def load_inventory(self, root_path: str, object_id: str) -> Inventory:
        d = self.staging_path(root_path, object_id)
        return msgspec.json.decode((d / "inventory.json").read_bytes(), type=Inventory)

    def save_inventory(
        self, root_path: str, object_id: str, inventory: Inventory
    ) -> None:
        d = self.staging_path(root_path, object_id)
        self._write_inventory(d, inventory)

    def load_content_map(self, root_path: str, object_id: str) -> dict:
        d = self.staging_path(root_path, object_id)
        return json.loads((d / "content.json").read_bytes())

    def save_content_map(
        self, root_path: str, object_id: str, content_map: dict
    ) -> None:
        d = self.staging_path(root_path, object_id)
        self._write_content_map(d, content_map)

    def purge(self, root_path: str, object_id: str) -> None:
        d = self.staging_path(root_path, object_id)
        if d.is_dir():
            shutil.rmtree(d)

    def _write_inventory(self, d: Path, inventory: Inventory) -> None:
        data = msgspec.json.encode(inventory)
        (d / "inventory.json").write_bytes(data)

    def _write_content_map(self, d: Path, content_map: dict) -> None:
        (d / "content.json").write_bytes(
            json.dumps(content_map, sort_keys=True).encode()
        )
