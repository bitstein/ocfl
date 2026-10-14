import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

import msgspec

from ocfl_lib import (
    DigestAlgorithm,
    FlatDirectLayout,
    Inventory,
    LocalFS,
    StorageRoot,
    StoredInventory,
    UserInfo,
    scan_dir,
)
from tests.helpers.ots import build_attestation


def write_files(dir_path: Path, files: Mapping[str, bytes | str]) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        target = dir_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            target.write_text(content)
        else:
            target.write_bytes(content)


class FixtureRoot:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.base.mkdir(parents=True, exist_ok=True)
        self.root_dir = base / "root"
        self.root_dir.mkdir()
        self.fs = LocalFS(self.root_dir)
        self.root = StorageRoot.init(self.fs, self.fs, layout=FlatDirectLayout())
        self._counter = 0

    def commit(
        self,
        object_id: str,
        files: Mapping[str, bytes | str],
        *,
        algorithm: DigestAlgorithm = DigestAlgorithm.SHA256,
        message: str | None = None,
        user: UserInfo | None = None,
        created: str = "2024-01-01T00:00:00+00:00",
    ) -> None:
        self._counter += 1
        src_name = f"src-{self._counter}"
        src_dir = self.base / src_name
        write_files(src_dir, files)

        stage = scan_dir(LocalFS(self.base), src_name, algorithm)
        self.root.commit(
            self.fs,
            object_id,
            stage,
            message=message,
            user=user,
            created=created,
        )

    def remove_object(self, object_id: str) -> None:
        self.root.purge(self.fs, object_id)

    @property
    def path(self) -> str:
        return str(self.root_dir)


def build_proof(payload: bytes, *, height: int | None = 854_321) -> bytes:
    return build_attestation(
        hashlib.sha256(payload).digest(),
        block_height=height,
        pending_uri="https://example.invalid" if height is None else None,
        merkle_step=True,
    )


def commit_with_inventory_proof(
    fixture: FixtureRoot,
    object_id: str,
    files: Mapping[str, bytes],
    *,
    version_num: int,
    height: int,
    version_key: str | None = None,
) -> None:
    fixture.commit(object_id, files, algorithm=DigestAlgorithm.SHA256)
    raw_version_key = version_key or f"v{version_num}"
    if raw_version_key != f"v{version_num}":
        rewrite_object_version_key(
            fixture,
            object_id,
            old_key=f"v{version_num}",
            new_key=raw_version_key,
        )

    relpath = fixture.root.object_path(object_id)
    inv_path = fixture.root_dir / relpath / raw_version_key / "inventory.json"
    payload = inv_path.read_bytes()
    proof = build_proof(payload, height=height)
    ext_dir = fixture.root_dir / relpath / "extensions" / "NNNN-ots-timestamps"
    ext_dir.mkdir(parents=True, exist_ok=True)
    (ext_dir / f"inventory.json.{raw_version_key}.ots").write_bytes(proof)


def rewrite_object_version_key(
    fixture: FixtureRoot,
    object_id: str,
    *,
    old_key: str,
    new_key: str,
) -> None:
    relpath = fixture.root.object_path(object_id)
    obj_dir = fixture.root_dir / relpath
    old_version_dir = obj_dir / old_key
    new_version_dir = obj_dir / new_key
    old_version_dir.rename(new_version_dir)

    _rewrite_inventory_version_key(obj_dir / "inventory.json", old_key, new_key)
    _rewrite_inventory_version_key(new_version_dir / "inventory.json", old_key, new_key)


def _rewrite_inventory_version_key(inv_path: Path, old_key: str, new_key: str) -> None:
    data = json.loads(inv_path.read_bytes())
    if data.get("head") == old_key:
        data["head"] = new_key
    versions = data["versions"]
    versions[new_key] = versions.pop(old_key)
    for paths in data["manifest"].values():
        for i, path in enumerate(paths):
            paths[i] = path.replace(f"{old_key}/", f"{new_key}/", 1)

    raw = json.dumps(data, ensure_ascii=False).encode()
    inv = msgspec.json.decode(raw, type=Inventory)
    stored = StoredInventory.from_inventory(inv, DigestAlgorithm.SHA256)
    inv_path.write_bytes(stored.raw_bytes)
    sidecar_path = inv_path.with_name(stored.sidecar_filename)
    sidecar_path.write_text(stored.sidecar_content)
