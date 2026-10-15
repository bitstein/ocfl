import hashlib
import json
import os
import shutil
from typing import TYPE_CHECKING

import msgspec

from ocfl_cli.errors import StagingError
from ocfl_cli.model.metadata import StagedMetadata, StagedUser
from ocfl_lib import Inventory

if TYPE_CHECKING:
    from pathlib import Path


class StagingStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def staging_path(self, root_path: str, object_id: str) -> Path:
        key = f"{root_path}\0{object_id}".encode()
        return self._root / hashlib.sha256(key).hexdigest()

    def exists(self, root_path: str, object_id: str) -> bool:
        return self.staging_path(root_path, object_id).is_dir()

    def object_ids(self, root_path: str) -> list[str]:
        if not self._root.is_dir():
            return []
        ids: list[str] = []
        for d in self._root.iterdir():
            inventory = d / "inventory.json"
            if not inventory.is_file():
                continue
            try:
                object_id = json.loads(inventory.read_bytes())["id"]
            except OSError, ValueError, KeyError, TypeError:
                continue
            if (
                isinstance(object_id, str)
                and self.staging_path(root_path, object_id) == d
            ):
                ids.append(object_id)
        return sorted(ids)

    def create(self, root_path: str, object_id: str, inventory: Inventory) -> None:
        d = self.staging_path(root_path, object_id)
        d.mkdir(parents=True, exist_ok=True)
        self._write_inventory(d, inventory)
        self._write_content_map(d, {})
        self._write_metadata(d, StagedMetadata())

    def load_inventory(self, root_path: str, object_id: str) -> Inventory:
        path = self.staging_path(root_path, object_id) / "inventory.json"
        try:
            return msgspec.json.decode(path.read_bytes(), type=Inventory)
        except msgspec.DecodeError as e:
            raise StagingError(f"{path}: {e}") from None

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

    def load_metadata(self, root_path: str, object_id: str) -> StagedMetadata:
        d = self.staging_path(root_path, object_id)
        path = d / "metadata.json"
        if not path.is_file():
            return StagedMetadata()
        try:
            metadata = msgspec.json.decode(path.read_bytes(), type=StagedMetadata)
        except (OSError, msgspec.DecodeError) as e:
            raise StagingError(f"{path}: {e}") from None
        _validate_metadata(metadata, path=path)
        return metadata

    def save_metadata(
        self, root_path: str, object_id: str, metadata: StagedMetadata
    ) -> None:
        d = self.staging_path(root_path, object_id)
        self._write_metadata(d, metadata)

    def purge(self, root_path: str, object_id: str) -> None:
        d = self.staging_path(root_path, object_id)
        if d.is_dir():
            shutil.rmtree(d)

    def _write_inventory(self, d: Path, inventory: Inventory) -> None:
        data = msgspec.json.encode(inventory)
        atomic_write_bytes(d / "inventory.json", data)

    def _write_content_map(self, d: Path, content_map: dict) -> None:
        data = json.dumps(content_map, sort_keys=True).encode()
        atomic_write_bytes(d / "content.json", data)

    def _write_metadata(self, d: Path, metadata: StagedMetadata) -> None:
        path = d / "metadata.json"
        _validate_metadata(metadata, path=path)
        data = msgspec.json.encode(metadata)
        atomic_write_bytes(path, data)


def _validate_metadata(metadata: StagedMetadata, *, path: Path) -> None:
    _validate_metadata_value(metadata.message, field="message", path=path)
    if metadata.user is None:
        return
    _validate_metadata_user(metadata.user, path=path)


def _validate_metadata_user(user: StagedUser, *, path: Path) -> None:
    _validate_metadata_value(user.name, field="user.name", path=path)
    _validate_metadata_value(user.address, field="user.address", path=path)


def _validate_metadata_value(value: str | None, *, field: str, path: Path) -> None:
    if value is not None and not value.strip():
        raise StagingError(f"{path}: staged metadata {field} cannot be empty")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
