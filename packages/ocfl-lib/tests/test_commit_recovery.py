"""Recovery of a commit that died part-way (ADR-0001), per backend."""

import hashlib
from io import BytesIO
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from ocfl_lib import (
    CommitState,
    DigestAlgorithm,
    LocalFS,
    RecoveryAction,
    RecoveryActionError,
    RecoveryRequiredError,
    RevertCompletedError,
    Spec,
    StorageRoot,
    get_layout,
    scan_dir,
)
from ocfl_lib.validation import validate_root

LAYOUT = "0004-hashed-n-tuple-storage-layout"
OID = "ark:/1/obj"
BUCKET = "commit-recovery"
R, V, S = RecoveryAction.RESUME, RecoveryAction.REVERT, RecoveryAction.REMOVE_STRAY


class Died(BaseException):
    """The writer process died: no undo runs."""


def _local_dies(root: Path, when):
    class Dies(LocalFS):
        def write(self, name, data):
            if when("write", name):
                raise Died(name)
            return super().write(name, data)

        def remove(self, name):
            if when("remove", name):
                raise Died(name)
            return super().remove(name)

        def rename_noreplace(self, src, dst):
            if when("rename", dst):
                raise Died(dst)
            return super().rename_noreplace(src, dst)

    return Dies(root)


def _s3_dies(client, when):
    from ocfl_lib.fs.s3 import S3FS

    class Dies(S3FS):
        def write(self, name, data):
            if when("write", name):
                raise Died(name)
            return super().write(name, data)

        def write_new(self, name, data):
            if when("write", name):
                raise Died(name)
            return super().write_new(name, data)

        def write_if_match(self, name, data, tag):
            if when("write", name):
                raise Died(name)
            return super().write_if_match(name, data, tag)

        def remove(self, name):
            if when("remove", name):
                raise Died(name)
            return super().remove(name)

    return Dies(BUCKET, client=client)


def _stage(base: Path, name: str, files: dict[str, bytes]):
    d = base / name
    for p, b in files.items():
        (d / p).parent.mkdir(parents=True, exist_ok=True)
        (d / p).write_bytes(b)
    return scan_dir(LocalFS(base), name, DigestAlgorithm.SHA512)


@pytest.fixture(params=["local", "s3"])
def backend(request, tmp_path):
    if request.param == "local":
        d = tmp_path / "root"
        d.mkdir()
        yield "local", LocalFS(d), lambda when: _local_dies(d, when)
        return
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        from ocfl_lib.fs.s3 import S3FS

        yield "s3", S3FS(BUCKET, client=client), lambda when: _s3_dies(client, when)


def _setup(fs, tmp_path, *, existing=True):
    StorageRoot.init(fs, fs, spec=Spec.V1_1, layout=get_layout(LAYOUT))
    root = StorageRoot.open(fs)
    if existing:
        root.commit(fs, OID, _stage(tmp_path, "s1", {"a.txt": b"a"}))
    vs = _stage(tmp_path, "s2", {"a.txt": b"a2", "b.txt": b"b"})
    plan = root.plan(OID, vs, created="2026-01-02T00:00:00Z")
    return root, plan, vs.content_source


def _head(root):
    obj = root.get_object(OID)
    return str(obj.head) if obj.exists else None


def _clean(fs, root):
    rep = validate_root(fs, "")
    assert rep.is_valid, [(f.code, f.path) for f in rep.errors]
    assert not [f for f in rep.findings if f.code.startswith("W016")]
    assert root.commit_status(OID).state is CommitState.CLEAN


def _claimed(fs, die, tmp_path):
    root, plan, content = _setup(fs, tmp_path)
    obj = root.object_path(OID)
    with pytest.raises(Died):
        root.commit(
            die(lambda o, n: o == "write" and n == f"{obj}/inventory.json"),
            OID,
            plan,
            content=content,
        )
    st = root.commit_status(OID)
    assert st.state is CommitState.CLAIMED and st.version == "v2"
    return root, plan, content, obj


class TestStates:
    def test_planned(self, backend, tmp_path):
        kind, fs, die = backend
        root, plan, content = _setup(fs, tmp_path)
        obj = root.object_path(OID)
        before = {r.path for r in fs.walk_files(obj)}
        target = f"{obj}/v2/inventory.json"
        with pytest.raises(Died):
            root.commit(
                die(lambda o, n: o == "write" and n.endswith("/build/content/a.txt"))
                if kind == "local"
                else die(lambda o, n: o == "write" and n == target),
                OID,
                plan,
                content=content,
            )
        assert root.commit_status(OID).state is CommitState.PLANNED
        assert {r.path for r in fs.walk_files(obj)} == before
        with pytest.raises(RecoveryRequiredError):
            root.commit(fs, OID, plan, content=content)
        root.recover(fs, OID, R)
        assert _head(root) == "v1"
        _clean(fs, root)
        root.commit(fs, OID, plan, content=content)
        assert _head(root) == "v2"

    @pytest.mark.parametrize("action", [R, V])
    def test_claimed(self, backend, tmp_path, action):
        _kind, fs, die = backend
        root, _plan, content, obj = _claimed(fs, die, tmp_path)
        st = root.recover(fs, OID, action, content=content)
        assert st.state is CommitState.CLAIMED
        assert _head(root) == ("v2" if action is R else "v1")
        assert not fs.exists(f"{obj}/v2") or action is R
        _clean(fs, root)

    def test_s3_resume_needs_content_if_missing(self, backend, tmp_path):
        kind, fs, die = backend
        if kind != "s3":
            pytest.skip("a local claim is complete")
        root, plan, content = _setup(fs, tmp_path)
        obj = root.object_path(OID)
        with pytest.raises(Died):
            root.commit(
                die(lambda o, n: o == "write" and n.startswith(f"{obj}/v2/content")),
                OID,
                plan,
                content=content,
            )
        assert root.commit_status(OID).state is CommitState.CLAIMED
        from ocfl_lib import ContentNotFound

        with pytest.raises(ContentNotFound):
            root.recover(fs, OID, R)
        root.recover(fs, OID, R, content=content)
        assert _head(root) == "v2"
        _clean(fs, root)

    def test_inventory_published_without_sidecar(self, backend, tmp_path):
        _kind, fs, die = backend
        root, plan, content = _setup(fs, tmp_path)
        obj = root.object_path(OID)
        with pytest.raises(Died):
            root.commit(
                die(lambda o, n: o == "write" and n == f"{obj}/inventory.json.sha512"),
                OID,
                plan,
                content=content,
            )
        st = root.commit_status(OID)
        assert (st.state, st.version) == (CommitState.INVENTORY_PUBLISHED, "v2")
        with pytest.raises(RevertCompletedError):
            root.recover(fs, OID, V)
        root.recover(fs, OID, R)
        assert _head(root) == "v2"
        _clean(fs, root)

    def test_published_leftover_plan(self, backend, tmp_path):
        _kind, fs, die = backend
        root, plan, content = _setup(fs, tmp_path)
        with pytest.raises(Died):
            root.commit(
                die(lambda o, n: o == "remove" and n.endswith("/plan.json")),
                OID,
                plan,
                content=content,
            )
        assert root.commit_status(OID).state is CommitState.PUBLISHED
        root.recover(fs, OID, R)
        _clean(fs, root)

    def test_stale_sidecar_without_plan_e060(self, backend, tmp_path):
        _kind, fs, _die = backend
        root, plan, content = _setup(fs, tmp_path)
        root.commit(fs, OID, plan, content=content)
        obj = root.object_path(OID)
        stale = f"{plan.base_digest} inventory.json\n".encode()
        fs.write(f"{obj}/inventory.json.sha512", BytesIO(stale))
        st = root.commit_status(OID)
        assert (st.state, st.version) == (CommitState.INVENTORY_PUBLISHED, "v2")
        root.recover(fs, OID, R)
        _clean(fs, root)

    def test_stray_claim(self, backend, tmp_path):
        _kind, fs, _die = backend
        root, plan, content = _setup(fs, tmp_path)
        root.commit(fs, OID, plan, content=content)
        obj = root.object_path(OID)
        for f in ("inventory.json", "inventory.json.sha512"):
            with fs.open_file(f"{obj}/v1/{f}") as src:
                fs.write(f"{obj}/{f}", BytesIO(src.read()))
        st = root.commit_status(OID)
        assert (st.state, st.version) == (CommitState.STRAY, "v2")
        for action in (R, V):
            with pytest.raises(RecoveryActionError):
                root.recover(fs, OID, action)
        with pytest.raises(RecoveryActionError):
            root.recover(fs, OID, S, version="v1")
        root.recover(fs, OID, S, version="v2")
        assert _head(root) == "v1"
        _clean(fs, root)

    def test_new_object_crash_prunes_empty_parents(self, tmp_path):
        d = tmp_path / "root"
        d.mkdir()
        fs = LocalFS(d)
        root, plan, content = _setup(fs, tmp_path, existing=False)
        obj = root.object_path(OID)
        with pytest.raises(Died):
            root.commit(
                _local_dies(d, lambda o, n: o == "rename" and n == obj),
                OID,
                plan,
                content=content,
            )
        top = d / obj.split("/")[0]
        assert top.is_dir()
        assert any(f.code.startswith("E073") for f in validate_root(fs, "").errors)
        root.recover(fs, OID, V)
        assert not top.exists()
        _clean(fs, root)

    def test_recover_twice_is_a_noop(self, backend, tmp_path):
        _kind, fs, die = backend
        root, _plan, _content, _obj = _claimed(fs, die, tmp_path)
        root.recover(fs, OID, V)
        snapshot = sorted(r.path for r in fs.walk_files(""))
        assert root.recover(fs, OID, V).state is CommitState.CLEAN
        assert sorted(r.path for r in fs.walk_files("")) == snapshot


class TestUnreadablePlan:
    def _corrupt(self, fs, body: bytes) -> None:
        from ocfl_lib._commit import work_dir

        fs.write(f"{work_dir('', OID)}/plan.json", BytesIO(body))

    @pytest.mark.parametrize("body", [b"{not json", b'{"planFormat": 99}'])
    def test_blocks_commit_and_purge_until_discarded(self, backend, tmp_path, body):
        _kind, fs, _die = backend
        root, plan, content = _setup(fs, tmp_path)
        self._corrupt(fs, body)
        assert root.commit_status(OID).state is CommitState.UNREADABLE_PLAN
        with pytest.raises(RecoveryRequiredError):
            root.commit(fs, OID, plan, content=content)
        with pytest.raises(RecoveryRequiredError):
            root.purge(fs, OID)
        with pytest.raises(RecoveryActionError):
            root.recover(fs, OID, S, version="v2")
        st = root.recover(fs, OID, V)
        assert st.state is CommitState.UNREADABLE_PLAN
        _clean(fs, root)
        root.commit(fs, OID, plan, content=content)
        assert _head(root) == "v2"

    def test_plan_for_another_object(self, backend, tmp_path):
        _kind, fs, _die = backend
        root, _plan, _content = _setup(fs, tmp_path)
        other = root.plan("ark:/1/other", _stage(tmp_path, "o", {"z": b"z"}))
        self._corrupt(fs, other.to_json())
        assert root.commit_status(OID).state is CommitState.UNREADABLE_PLAN
        root.recover(fs, OID, R)
        _clean(fs, root)

    def test_stray_claim_shows_after_discard(self, backend, tmp_path):
        _kind, fs, die = backend
        root, _plan, _content, _obj = _claimed(fs, die, tmp_path)
        self._corrupt(fs, b"garbage")
        root.recover(fs, OID, V)
        st = root.commit_status(OID)
        assert (st.state, st.version) == (CommitState.STRAY, "v2")
        root.recover(fs, OID, S, version="v2")
        assert _head(root) == "v1"
        _clean(fs, root)


def test_key_is_sha256_of_object_id():
    from ocfl_lib._commit import work_dir

    assert work_dir("", OID).endswith(hashlib.sha256(OID.encode()).hexdigest())
