"""Shared helpers for ocfl-index tests."""

import hashlib
from collections.abc import Mapping
from pathlib import Path

from opentimestamps.core.notary import (
    BitcoinBlockHeaderAttestation,
    PendingAttestation,
)
from opentimestamps.core.op import OpSHA256
from opentimestamps.core.serialize import BytesSerializationContext
from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

from ocfl_lib import (
    DigestAlgorithm,
    LocalFS,
    StorageRoot,
    UserInfo,
    stage_dir,
)
from ocfl_lib.layout import FlatDirectLayout


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
    """Helper to build, mutate, and inspect a local OCFL storage root."""

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

        stage = stage_dir(LocalFS(self.base), src_name, algorithm)
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
    file_hash = hashlib.sha256(payload).digest()
    ts = Timestamp(file_hash)
    sub = ts.ops.add(OpSHA256())
    if height is None:
        sub.attestations.add(PendingAttestation("https://example.invalid"))
    else:
        sub.attestations.add(BitcoinBlockHeaderAttestation(height))
    dtsf = DetachedTimestampFile(OpSHA256(), ts)
    ctx = BytesSerializationContext()
    dtsf.serialize(ctx)
    return ctx.getbytes()


def commit_with_inventory_proof(
    fixture: FixtureRoot,
    object_id: str,
    files: Mapping[str, bytes],
    *,
    version_num: int,
    height: int,
) -> None:
    fixture.commit(object_id, files, algorithm=DigestAlgorithm.SHA256)
    relpath = fixture.root.object_path(object_id)
    inv_path = fixture.root_dir / relpath / f"v{version_num}" / "inventory.json"
    payload = inv_path.read_bytes()
    proof = build_proof(payload, height=height)
    ext_dir = fixture.root_dir / relpath / "extensions" / "NNNN-ots-timestamps"
    ext_dir.mkdir(parents=True, exist_ok=True)
    (ext_dir / f"inventory.json.v{version_num}.ots").write_bytes(proof)
