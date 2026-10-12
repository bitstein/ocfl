"""StorageRoot.commit and the OTS extension: no commit publishes an invalid version."""

import dataclasses
import hashlib
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from ocfl_lib import (
    CommitState,
    DigestAlgorithm,
    LocalFS,
    MissingContentProofError,
    MissingInventoryProofError,
    OtsConfigError,
    PlanMismatchError,
    RecoveryAction,
    RevertCompletedError,
    Spec,
    StorageRoot,
    get_layout,
    ots,
    scan_dir,
)
from ocfl_lib.fs import read_all, write_bytes
from ocfl_lib.validation import validate_root
from tests.helpers.faults import Crash, fail_at
from tests.helpers.ots import build_attestation

OID = "obj"
FILES = {"a.txt": b"alpha", "d/b.txt": b"beta"}


def proved(files: dict[str, bytes]) -> dict[str, bytes]:
    out = dict(files)
    for p, b in files.items():
        out[f"{p}.ots"] = build_attestation(hashlib.sha256(b).digest(), block_height=1)
    return out


class Root:
    def __init__(self, fs, tmp: Path) -> None:
        self.fs = fs
        self.tmp = tmp
        self.n = 0
        layout = get_layout("0002-flat-direct-storage-layout")
        StorageRoot.init(fs, fs, spec=Spec.V1_1, layout=layout)
        self.root = StorageRoot.open(fs)

    def plan(self, files, *, proof: bool, alg=DigestAlgorithm.SHA256):
        self.n += 1
        src = self.tmp / f"src{self.n}"
        for p, b in files.items():
            (src / p).parent.mkdir(parents=True, exist_ok=True)
            (src / p).write_bytes(b)
        vs = scan_dir(LocalFS(self.tmp), src.name, alg)
        plan = self.root.plan(OID, vs)
        if proof:
            digest = hashlib.sha256(plan.inventory.raw_bytes).digest()
            proof_bytes = build_attestation(digest, block_height=1)
            plan = dataclasses.replace(plan, inventory_proof=proof_bytes)
        return plan, vs

    def commit(self, files, *, proof: bool, alg=DigestAlgorithm.SHA256):
        plan, vs = self.plan(files, proof=proof, alg=alg)
        self.root.commit(self.fs, OID, plan, content=vs.content_source)

    def read(self, rel: str) -> bytes | None:
        try:
            return read_all(self.fs, f"{self.root.object_path(OID)}/{rel}")
        except FileNotFoundError:
            return None

    def assert_clean(self) -> None:
        assert self.root.commit_status(OID).state is CommitState.CLEAN
        report = validate_root(self.fs, "")
        assert report.is_valid, [(f.code, dict(f.detail)) for f in report.errors]
        assert not [f for f in report.findings if f.code == "W016"]


@pytest.fixture(params=["local", "s3"])
def root(request, tmp_path, monkeypatch):
    if request.param == "local":
        (tmp_path / "root").mkdir()
        yield Root(LocalFS(tmp_path / "root"), tmp_path)
        return
    from ocfl_lib.fs.s3 import S3FS

    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="ots-commit")
        yield Root(S3FS("ots-commit", client=client), tmp_path)


# --- 4.4 refuse a version the extension rejects


def test_refuses_version_without_inventory_proof(root):
    root.commit(proved(FILES), proof=True)
    before = root.read("inventory.json")
    with pytest.raises(MissingInventoryProofError, match="E904"):
        root.commit(proved({**FILES, "c": b"c"}), proof=False)
    assert root.read("inventory.json") == before
    assert root.read("v2/inventory.json") is None
    root.assert_clean()


def test_refuses_path_without_content_proof(root):
    root.commit(proved(FILES), proof=True)
    v2 = proved({**FILES, "c": b"c"})
    del v2["c.ots"]
    with pytest.raises(MissingContentProofError, match="E902"):
        root.commit(v2, proof=True)
    assert root.read("v2/inventory.json") is None
    root.assert_clean()


def test_refuses_new_object_without_content_proofs(root):
    with pytest.raises(MissingContentProofError):
        root.commit(FILES, proof=True)
    assert root.read("inventory.json") is None
    root.assert_clean()


def test_refuses_on_missing_config(root):
    root.commit(proved(FILES), proof=True)
    root.fs.remove(f"{root.root.object_path(OID)}/{ots.CONFIG_PATH}")
    with pytest.raises(OtsConfigError):
        root.commit(proved({**FILES, "c": b"c"}), proof=True)
    assert root.read("v2/inventory.json") is None


def test_flags_off_need_no_proofs(root):
    root.commit(proved(FILES), proof=True)
    cfg = ots.OtsConfig(timestamp_content=False, timestamp_inventory=False)
    write_bytes(
        root.fs, f"{root.root.object_path(OID)}/{ots.CONFIG_PATH}", cfg.to_json()
    )
    root.commit({**FILES, "c": b"c"}, proof=False)
    root.assert_clean()


def test_refuses_sha512_on_a_timestamped_object(root):
    root.commit(proved(FILES), proof=True)
    cfg = ots.OtsConfig(timestamp_content=False, timestamp_inventory=False)
    write_bytes(
        root.fs, f"{root.root.object_path(OID)}/{ots.CONFIG_PATH}", cfg.to_json()
    )
    with pytest.raises(PlanMismatchError, match="E901"):
        root.commit(FILES, proof=False, alg=DigestAlgorithm.SHA512)
    root.assert_clean()


def test_proof_and_content_proofs_commit(root):
    root.commit(proved(FILES), proof=True)
    root.commit(proved({**FILES, "c": b"c"}), proof=True)
    assert root.read(ots.inventory_proof_path("v2")) is not None
    root.assert_clean()


# --- 4.5 an existing object starts timestamping


def test_start_on_existing_object(root):
    root.commit(FILES, proof=False)
    root.commit(proved(FILES), proof=True)
    raw = root.read(ots.CONFIG_PATH)
    assert raw is not None
    assert ots.OtsConfig.from_json(raw) == ots.OtsConfig(timestamp_from="v2")
    assert root.read(ots.inventory_proof_path("v1")) is None
    assert root.read(ots.inventory_proof_path("v2")) is not None
    root.assert_clean()
    with pytest.raises(MissingInventoryProofError):
        root.commit(proved(FILES), proof=False)
    root.commit(proved({**FILES, "c": b"c"}), proof=True)
    root.assert_clean()


def test_start_needs_proofs_for_carried_files(root):
    root.commit(FILES, proof=False)
    with pytest.raises(MissingContentProofError, match=r"a\.txt"):
        root.commit({**FILES, **proved({"c": b"c"})}, proof=True)
    assert root.read(ots.CONFIG_PATH) is None
    root.assert_clean()


def test_start_refuses_sha512_history(root):
    root.commit(FILES, proof=False, alg=DigestAlgorithm.SHA512)
    with pytest.raises(PlanMismatchError):
        root.commit(proved(FILES), proof=True)
    assert root.read(ots.CONFIG_PATH) is None


def test_new_object_config_has_no_timestamp_from(root):
    root.commit(proved(FILES), proof=True)
    raw = root.read(ots.CONFIG_PATH)
    assert raw is not None and ots.OtsConfig.from_json(raw) == ots.OtsConfig()


@pytest.mark.parametrize("action", [RecoveryAction.RESUME, RecoveryAction.REVERT])
def test_start_crash_at_every_write(root, action):
    """Each partial state of a starting commit (claimed; config; config and
    proof; root inventory) recovers to v1 without the extension or to v2
    with it."""
    seen = set()
    for n in range(1, 60):
        if root.root.get_object(OID).exists:
            root.root.purge(root.fs, OID)
        root.commit(FILES, proof=False)
        plan, vs = root.plan(proved(FILES), proof=True)
        writer, _ = fail_at(root.fs, n)
        try:
            root.root.commit(writer, OID, plan, content=vs.content_source)
        except Crash:
            pass
        else:
            break
        seen.add(
            (
                root.root.commit_status(OID).state,
                root.read(ots.CONFIG_PATH) is not None,
                root.read(ots.inventory_proof_path("v2")) is not None,
            )
        )
        try:
            root.root.recover(root.fs, OID, action, content=vs.content_source)
        except RevertCompletedError:
            root.root.recover(root.fs, OID, RecoveryAction.RESUME)
        root.assert_clean()
        obj = root.root.object_path(OID)
        if str(root.root.get_object(OID).head) == "v2":
            assert root.read(ots.CONFIG_PATH) is not None
            assert root.read(ots.inventory_proof_path("v2")) is not None
        else:
            assert not root.fs.exists(f"{obj}/{ots.EXTENSION_DIR}")
    else:
        pytest.fail("commit never finished")
    assert (CommitState.CLAIMED, True, False) in seen  # config, no proof yet
    assert (CommitState.CLAIMED, True, True) in seen  # config and proof
