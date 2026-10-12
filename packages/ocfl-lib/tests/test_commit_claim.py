"""Commit by claim, then publish (ADR-0001): write order and lost claims."""

import dataclasses
import hashlib
from io import BytesIO
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from ocfl_lib import (
    ClaimLostError,
    DigestAlgorithm,
    ErrorClass,
    LocalFS,
    ObjectLockedError,
    RecoveryRequiredError,
    Spec,
    StorageRoot,
    UnsupportedBackendError,
    get_layout,
    scan_dir,
)
from ocfl_lib.validation import validate_root
from tests.helpers.ots import build_attestation

LAYOUT = "0004-hashed-n-tuple-storage-layout"
OTS = "extensions/NNNN-ots-timestamps"
OID = "ark:/1/obj"
BUCKET = "commit-claim"


def _stage(base: Path, name: str, files: dict[str, bytes], alg=DigestAlgorithm.SHA256):
    d = base / name
    for p, b in files.items():
        (d / p).parent.mkdir(parents=True, exist_ok=True)
        (d / p).write_bytes(b)
    return scan_dir(LocalFS(base), name, alg)


def _ots(files: dict[str, bytes]) -> dict[str, bytes]:
    out = dict(files)
    for p, b in files.items():
        out[f"{p}.ots"] = build_attestation(
            hashlib.sha256(b).digest(), block_height=100
        )
    return out


def _with_proof(plan):
    proof = build_attestation(
        hashlib.sha256(plan.inventory.raw_bytes).digest(), block_height=101
    )
    return dataclasses.replace(plan, inventory_proof=proof)


def _root(fs) -> StorageRoot:
    StorageRoot.init(fs, fs, spec=Spec.V1_1, layout=get_layout(LAYOUT))
    return StorageRoot.open(fs)


def _ots_object(fs, src: Path):
    """An OTS object at v1 and the plan (with proof) for its v2."""
    root = _root(fs)
    vs1 = _stage(src, "s1", _ots({"a.txt": b"a"}))
    p1 = _with_proof(root.plan(OID, vs1, created="2026-01-01T00:00:00Z"))
    root.commit(fs, OID, p1, content=vs1.content_source)
    vs2 = _stage(src, "s2", _ots({"a.txt": b"a", "b.txt": b"b"}))
    p2 = _with_proof(root.plan(OID, vs2, created="2026-01-02T00:00:00Z"))
    return root, p2, vs2.content_source


class Recorder(LocalFS):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.ops: list[tuple[str, ...]] = []

    def write(self, name, data):
        self.ops.append(("write", name))
        return super().write(name, data)

    def remove(self, name):
        self.ops.append(("remove", name))
        return super().remove(name)

    def remove_all(self, name):
        self.ops.append(("remove_all", name))
        return super().remove_all(name)

    def rename_noreplace(self, src, dst):
        self.ops.append(("rename_noreplace", src, dst))
        return super().rename_noreplace(src, dst)


def _inside(obj: str, op: tuple[str, ...]) -> bool:
    return any(p == obj or p.startswith(obj + "/") for p in op[1:])


class TestLocal:
    def test_existing_object_claims_then_proof_inventory_sidecar(self, tmp_path):
        d = tmp_path / "root"
        d.mkdir()
        root, plan, content = _ots_object(LocalFS(d), tmp_path)
        obj = root.object_path(OID)
        rec = Recorder(d)
        root.commit(rec, OID, plan, content=content)

        ops = [op for op in rec.ops if _inside(obj, op)]
        assert ops[0][0] == "rename_noreplace"
        assert ops[0][2] == f"{obj}/v2"
        assert ops[1:] == [
            ("write", f"{obj}/{OTS}/inventory.json.v2.ots"),
            ("write", f"{obj}/inventory.json"),
            ("write", f"{obj}/inventory.json.sha256"),
        ]
        assert validate_root(LocalFS(d), "").is_valid
        assert not (d / "extensions" / "NNNN-work-dir").exists()

    def test_new_object_is_one_rename(self, tmp_path):
        d = tmp_path / "root"
        d.mkdir()
        rec = Recorder(d)
        root = _root(rec)
        vs = _stage(tmp_path, "s", _ots({"a.txt": b"a"}))
        plan = _with_proof(root.plan(OID, vs))
        obj = root.object_path(OID)
        rec.ops.clear()
        root.commit(rec, OID, plan, content=vs.content_source)
        assert [op for op in rec.ops if _inside(obj, op)] == [
            ("rename_noreplace", f"extensions/NNNN-work-dir/{_key()}/build", obj)
        ]
        assert (d / obj / OTS / "config.json").is_file()
        assert validate_root(LocalFS(d), "").is_valid

    @pytest.mark.parametrize("new", [True, False])
    def test_lost_claim_writes_nothing_into_object(self, tmp_path, new):
        d = tmp_path / "root"
        d.mkdir()
        fs = LocalFS(d)
        root = _root(fs)
        if not new:
            root.commit(fs, OID, _stage(tmp_path, "s0", {"x": b"x"}))
        vs = _stage(tmp_path, "s", {"a.txt": b"a"})
        plan = root.plan(OID, vs)
        target = d / root.object_path(OID)
        if not new:
            target = target / "v2"
        before = sorted(p for p in d.rglob("*") if p.is_file())

        class OtherWriter:
            def get_content(self, digest):
                target.mkdir(parents=True, exist_ok=True)
                return vs.content_source.get_content(digest)

        with pytest.raises(ClaimLostError) as ei:
            root.commit(fs, OID, plan, content=OtherWriter())
        assert ei.value.error_class is ErrorClass.STATE
        assert sorted(p for p in d.rglob("*") if p.is_file()) == before
        assert not any(target.iterdir())

    def test_empty_target_is_not_replaced(self, tmp_path):
        fs = LocalFS(tmp_path)
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "f").write_text("x")
        (tmp_path / "b").mkdir()
        with pytest.raises(FileExistsError):
            fs.rename_noreplace("a", "b")
        fs.rename_noreplace("a", "c")
        assert (tmp_path / "c" / "f").is_file()

    def test_busy_lock(self, tmp_path, monkeypatch):
        import ocfl_lib._commit as commit_mod

        monkeypatch.setattr(commit_mod, "LOCK_TIMEOUT", 0.05)
        fs = LocalFS(tmp_path)
        root = _root(fs)
        vs = _stage(tmp_path, "s", {"a.txt": b"a"})
        with (
            fs.lock(commit_mod.work_dir("", OID), timeout=0),
            pytest.raises(ObjectLockedError),
        ):
            root.commit(fs, OID, vs)

    def test_lock_survives_removal_of_its_dir(self, tmp_path):
        fs = LocalFS(tmp_path)
        with fs.lock("w/x", timeout=0):
            assert fs.remove_empty_dir("w/x")
        with fs.lock("w/x", timeout=0):
            assert (tmp_path / "w" / "x").is_dir()

    def test_stray_claim_blocks_commit(self, tmp_path):
        fs = LocalFS(tmp_path)
        root = _root(fs)
        root.commit(fs, OID, _stage(tmp_path, "s0", {"x": b"x"}))
        (tmp_path / root.object_path(OID) / "v2").mkdir()
        with pytest.raises(RecoveryRequiredError):
            root.commit(fs, OID, _stage(tmp_path, "s", {"a.txt": b"a"}))

    def test_stale_proof_removed_before_publish(self, tmp_path):
        d = tmp_path / "root"
        d.mkdir()
        fs = LocalFS(d)
        root, plan, content = _ots_object(fs, tmp_path)
        stale = d / root.object_path(OID) / OTS / "inventory.json.v2.ots"
        stale.write_bytes(b"stale")
        root.commit(fs, OID, plan, content=content)
        assert stale.read_bytes() == plan.inventory_proof

    def test_plain_writefs_refused(self, tmp_path):
        fs = LocalFS(tmp_path)
        root = _root(fs)

        class Plain:
            def open_file(self, n):
                return fs.open_file(n)

            def read_dir(self, n):
                return fs.read_dir(n)

            def exists(self, n):
                return fs.exists(n)

            def write(self, n, d):
                return fs.write(n, d)

            def remove(self, n):
                return fs.remove(n)

            def remove_all(self, n):
                return fs.remove_all(n)

            def mkdir(self, n):
                return fs.mkdir(n)

        vs = _stage(tmp_path, "s", {"a.txt": b"a"})
        with pytest.raises(UnsupportedBackendError):
            root.commit(Plain(), OID, vs)  # ty: ignore[invalid-argument-type]
        assert not root.get_object(OID).exists


def _key() -> str:
    return hashlib.sha256(OID.encode()).hexdigest()


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


def _record(client, log):
    def rec(params, model, **_kw):
        if model.name in ("PutObject", "DeleteObject"):
            log.append(
                (
                    model.name,
                    params["Key"],
                    "IfNoneMatch" in params,
                    "IfMatch" in params,
                )
            )

    client.meta.events.register("before-parameter-build.s3", rec)
    return rec


class TestS3:
    def test_existing_object_order(self, s3, tmp_path):
        from ocfl_lib.fs.s3 import S3FS

        fs = S3FS(BUCKET, client=s3)
        root, plan, content = _ots_object(fs, tmp_path)
        obj = root.object_path(OID)
        log: list = []
        rec = _record(s3, log)
        try:
            root.commit(fs, OID, plan, content=content)
        finally:
            s3.meta.events.unregister("before-parameter-build.s3", rec)
        ops = [e for e in log if e[1].startswith(obj + "/")]
        assert ops[0] == ("PutObject", f"{obj}/v2/inventory.json", True, False)
        assert ops[-3:] == [
            ("PutObject", f"{obj}/{OTS}/inventory.json.v2.ots", False, False),
            ("PutObject", f"{obj}/inventory.json", False, True),
            ("PutObject", f"{obj}/inventory.json.sha256", False, False),
        ]
        assert validate_root(fs, "").is_valid
        assert not list(fs.walk_files("extensions/NNNN-work-dir"))

    def test_new_object_order(self, s3, tmp_path):
        from ocfl_lib.fs.s3 import S3FS

        fs = S3FS(BUCKET, client=s3)
        root = _root(fs)
        vs = _stage(tmp_path, "s", {"a.txt": b"a"})
        obj = root.object_path(OID)
        log: list = []
        rec = _record(s3, log)
        try:
            root.commit(fs, OID, vs)
        finally:
            s3.meta.events.unregister("before-parameter-build.s3", rec)
        keys = [k for _n, k, _a, _b in log if k.startswith(obj + "/")]
        assert keys[0] == f"{obj}/v1/inventory.json"
        assert keys[1] == f"{obj}/0=ocfl_object_1.1"
        assert keys[-2:] == [f"{obj}/inventory.json", f"{obj}/inventory.json.sha256"]
        assert validate_root(fs, "").is_valid

    def test_lost_claim(self, s3, tmp_path):
        from ocfl_lib.fs.s3 import S3FS

        fs = S3FS(BUCKET, client=s3)
        root = _root(fs)
        root.commit(fs, OID, _stage(tmp_path, "s0", {"x": b"x"}))
        claim = f"{root.object_path(OID)}/v2/inventory.json"

        raced: list[bool] = []

        def race(params, model, **_kw):
            if model.name == "PutObject" and params["Key"] == claim and not raced:
                raced.append(True)
                s3.put_object(Bucket=BUCKET, Key=claim, Body=b"other")

        s3.meta.events.register_first("before-parameter-build.s3", race)
        try:
            with pytest.raises(ClaimLostError):
                root.commit(fs, OID, _stage(tmp_path, "s", {"a.txt": b"a"}))
        finally:
            s3.meta.events.unregister("before-parameter-build.s3", race)
        keys = {r.path for r in fs.walk_files(root.object_path(OID))}
        assert {k for k in keys if "/v2/" in k} == {claim}
        assert not list(fs.walk_files("extensions/NNNN-work-dir"))

    def test_store_ignoring_conditions_is_refused(self, s3, tmp_path):
        from ocfl_lib.fs.s3 import S3FS

        fs = S3FS(BUCKET, client=s3)
        root = _root(fs)

        def strip(params, model, **_kw):
            if model.name == "PutObject":
                params.pop("IfNoneMatch", None)
                params.pop("IfMatch", None)

        s3.meta.events.register("before-parameter-build.s3", strip)
        try:
            with pytest.raises(UnsupportedBackendError):
                root.commit(fs, OID, _stage(tmp_path, "s", {"a.txt": b"a"}))
        finally:
            s3.meta.events.unregister("before-parameter-build.s3", strip)
        assert not root.get_object(OID).exists
        assert not list(fs.walk_files("extensions/NNNN-work-dir"))

    def test_conditional_writes(self, s3):
        from ocfl_lib.fs.s3 import S3FS

        fs = S3FS(BUCKET, client=s3)
        tag = fs.write_new("k", b"1")
        with pytest.raises(FileExistsError):
            fs.write_new("k", b"2")
        assert fs.write_if_match("k", b"3", '"nope"') is None
        new_tag = fs.write_if_match("k", b"4", tag)
        assert new_tag is not None
        assert fs.read_tagged("k") == (b"4", new_tag)
        fs.check_conditional_writes("probe")
        assert not fs.exists("probe")
        fs.write("w", BytesIO(b"x"))
