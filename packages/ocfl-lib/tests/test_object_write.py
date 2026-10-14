from typing import TYPE_CHECKING

import pytest

from ocfl_lib import (
    CommitPlan,
    DigestAlgorithm,
    LocalFS,
    Object,
    PlanMismatchError,
    Spec,
    StaleHeadError,
    StorageRoot,
    UserInfo,
    VersionState,
    VNum,
    get_layout,
    scan_dir,
)
from ocfl_lib.validation import validate_object

if TYPE_CHECKING:
    from pathlib import Path

OID = "test:1"


def _make_src_dir(tmp_path: Path, files: dict[str, str], name: str = "src") -> Path:
    """Create a source directory with given files."""
    src = tmp_path / name
    src.mkdir()
    for path, content in files.items():
        p = src / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return src


def _stage(tmp_path: Path, files: dict[str, str], name: str = "src") -> VersionState:
    src = _make_src_dir(tmp_path, files, name)
    return scan_dir(LocalFS(src.parent), src.name, DigestAlgorithm.SHA512)


def _root(tmp_path: Path) -> tuple[LocalFS, StorageRoot]:
    d = tmp_path / "root"
    d.mkdir()
    fs = LocalFS(d)
    layout = get_layout("0002-flat-direct-storage-layout")
    return fs, StorageRoot.init(fs, fs, spec=Spec.V1_1, layout=layout)


def _valid(fs: LocalFS, obj: Object) -> None:
    result = validate_object(fs, obj.path)
    assert result.is_valid, f"errors: {[e.render() for e in result.errors]}"


class _FailingSource:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def get_content(self, digest: str) -> tuple:
        raise self.exc


class TestCommit:
    def test_create_new_object(self, tmp_path: Path):
        fs, root = _root(tmp_path)
        assert not root.get_object(OID).exists
        obj = root.commit(
            fs,
            OID,
            _stage(tmp_path, {"file.txt": "hello"}),
            message="initial commit",
            user=UserInfo(name="Test User"),
            created="2024-01-01T00:00:00Z",
        )
        assert obj.exists
        assert obj.id == OID
        assert obj.head == VNum(1)
        assert obj.spec == Spec.V1_1
        _valid(fs, obj)

    def test_create_object_reads_back_content(self, tmp_path: Path):
        fs, root = _root(tmp_path)
        obj = root.commit(fs, OID, _stage(tmp_path, {"file.txt": "hello world"}))
        with obj.open_logical_path(VNum(1), "file.txt") as f:
            assert f.read() == b"hello world"

    def test_add_version_to_existing(self, tmp_path: Path):
        fs, root = _root(tmp_path)
        root.commit(fs, OID, _stage(tmp_path, {"file.txt": "v1 content"}))
        stage2 = _stage(
            tmp_path, {"file.txt": "v2 content", "new.txt": "brand new"}, "src2"
        )
        obj = root.commit(fs, OID, stage2, message="second version")
        assert obj.head == VNum(2)
        assert len(obj.versions()) == 2
        _valid(fs, obj)

    def test_forward_delta_dedup(self, tmp_path: Path):
        fs, root = _root(tmp_path)
        root.commit(fs, OID, _stage(tmp_path, {"a.txt": "same", "b.txt": "old"}))
        stage2 = _stage(tmp_path, {"a.txt": "same", "b.txt": "new"}, "src2")
        obj = root.commit(fs, OID, stage2)
        inv = obj.inventory.inventory
        v2_content_paths = [
            p for _d, ps in inv.manifest.items() for p in ps if p.startswith("v2/")
        ]
        assert len(v2_content_paths) == 1
        assert "b.txt" in v2_content_paths[0]

    def test_create_with_fixity(self, tmp_path: Path):
        fs, root = _root(tmp_path)
        src = _make_src_dir(tmp_path, {"file.txt": "hello"})
        stage = scan_dir(
            LocalFS(src.parent),
            "src",
            DigestAlgorithm.SHA512,
            fixity_algs=[DigestAlgorithm.MD5],
        )
        obj = root.commit(fs, OID, stage)
        inv = obj.inventory.inventory
        assert inv.fixity is not None
        assert "md5" in inv.fixity
        _valid(fs, obj)

    def test_error_reverts_on_failure(self, tmp_path: Path):
        fs, root = _root(tmp_path)
        stage = VersionState(
            state={"abc123": ["file.txt"]},
            digest_algorithm=DigestAlgorithm.SHA512,
            content_source=_FailingSource(OSError("disk full")),
        )
        with pytest.raises(OSError, match="disk full"):
            root.commit(fs, OID, stage)
        assert not (tmp_path / "root" / OID).exists()

    def test_keyboard_interrupt_reverts_then_propagates(self, tmp_path: Path):
        fs, root = _root(tmp_path)
        stage = VersionState(
            state={"abc123": ["file.txt"]},
            digest_algorithm=DigestAlgorithm.SHA512,
            content_source=_FailingSource(KeyboardInterrupt()),
        )
        with pytest.raises(KeyboardInterrupt):
            root.commit(fs, OID, stage)
        assert not (tmp_path / "root" / OID).exists()

    def test_keyboard_interrupt_v2_preserves_root_inventory(self, tmp_path: Path):
        fs, root = _root(tmp_path)
        root.commit(fs, OID, _stage(tmp_path, {"file.txt": "v1-content"}))
        obj_dir = tmp_path / "root" / OID
        v1_inv_bytes = (obj_dir / "inventory.json").read_bytes()
        v1_sidecar_bytes = (obj_dir / "inventory.json.sha512").read_bytes()

        bad_stage = VersionState(
            state={"deadbeef": ["new.txt"]},
            digest_algorithm=DigestAlgorithm.SHA512,
            content_source=_FailingSource(KeyboardInterrupt()),
        )
        with pytest.raises(KeyboardInterrupt):
            root.commit(fs, OID, bad_stage, message="v2")

        assert (obj_dir / "inventory.json").read_bytes() == v1_inv_bytes
        assert (obj_dir / "inventory.json.sha512").read_bytes() == v1_sidecar_bytes
        assert not (obj_dir / "v2").exists()

    def test_multiple_files(self, tmp_path: Path):
        fs, root = _root(tmp_path)
        stage = _stage(
            tmp_path, {"a.txt": "aaa", "dir/b.txt": "bbb", "dir/sub/c.txt": "ccc"}
        )
        obj = root.commit(fs, OID, stage)
        state = obj.version_info(VNum(1)).state
        all_paths = sorted(p for ps in state.values() for p in ps)
        assert all_paths == ["a.txt", "dir/b.txt", "dir/sub/c.txt"]
        _valid(fs, obj)

    def test_version_info_preserved(self, tmp_path: Path):
        fs, root = _root(tmp_path)
        user = UserInfo(name="Alice", address="mailto:alice@example.org")
        obj = root.commit(
            fs,
            OID,
            _stage(tmp_path, {"file.txt": "hello"}),
            message="test msg",
            user=user,
            created="2024-01-01T00:00:00Z",
        )
        info = obj.version_info(VNum(1))
        assert info.message == "test msg"
        assert info.user is not None
        assert info.user.name == "Alice"
        assert info.user.address == "mailto:alice@example.org"
        assert info.created == "2024-01-01T00:00:00Z"

    def test_returns_fresh_object(self, tmp_path: Path):
        fs, root = _root(tmp_path)
        obj = root.commit(fs, OID, _stage(tmp_path, {"file.txt": "v1"}))
        assert obj.head == VNum(1)
        obj = root.commit(fs, OID, _stage(tmp_path, {"file.txt": "v2"}, "src2"))
        assert obj.head == VNum(2)
        assert obj.inventory.inventory.head == "v2"
        _valid(fs, obj)


class TestPlanThenCommit:
    def test_plan_writes_nothing(self, tmp_path: Path):
        _fs, root = _root(tmp_path)
        plan = root.plan(OID, _stage(tmp_path, {"file.txt": "hello"}))
        assert plan.version == "v1"
        assert not root.get_object(OID).exists

    def test_create_via_plan(self, tmp_path: Path):
        fs, root = _root(tmp_path)
        stage = _stage(tmp_path, {"file.txt": "hello"})
        plan = root.plan(OID, stage, created="2024-01-01T00:00:00Z")
        obj = root.commit(fs, OID, plan, content=stage.content_source)
        assert obj.head == VNum(1)
        _valid(fs, obj)

    def test_plan_survives_json(self, tmp_path: Path):
        fs, root = _root(tmp_path)
        stage = _stage(tmp_path, {"file.txt": "hello"})
        plan = root.plan(OID, stage, created="2024-01-01T00:00:00Z")
        plan2 = CommitPlan.from_json(plan.to_json())
        assert plan2 == plan
        obj = root.commit(fs, OID, plan2, content=stage.content_source)
        _valid(fs, obj)

    def test_v2_via_plan(self, tmp_path: Path):
        fs, root = _root(tmp_path)
        root.commit(fs, OID, _stage(tmp_path, {"file.txt": "v1"}))
        stage2 = _stage(tmp_path, {"file.txt": "v2"}, "src2")
        plan = root.plan(OID, stage2, created="2024-06-01T00:00:00Z")
        obj = root.commit(fs, OID, plan, content=stage2.content_source)
        assert obj.head == VNum(2)
        _valid(fs, obj)

    def test_metadata_belongs_to_plan(self, tmp_path: Path):
        fs, root = _root(tmp_path)
        stage = _stage(tmp_path, {"file.txt": "hello"})
        plan = root.plan(OID, stage)
        with pytest.raises(PlanMismatchError):
            root.commit(fs, OID, plan, content=stage.content_source, message="m")

    def test_dropping_paths_needs_no_content(self, tmp_path: Path):
        fs, root = _root(tmp_path)
        obj = root.commit(fs, OID, _stage(tmp_path, {"a.txt": "a", "b.txt": "b"}))
        v1 = obj.version_state(VNum(1))
        keep = {d: ps for d, ps in v1.state.items() if "a.txt" in ps}
        stage2 = VersionState(
            state=keep,
            digest_algorithm=DigestAlgorithm.SHA512,
            content_source=_FailingSource(AssertionError("no content needed")),
        )
        obj = root.commit(fs, OID, root.plan(OID, stage2))
        assert obj.head == VNum(2)
        _valid(fs, obj)

    def test_independently_built_stale_plan_cannot_publish(self, tmp_path: Path):
        fs, root = _root(tmp_path)
        root.commit(fs, OID, _stage(tmp_path, {"base.txt": "v1"}))
        stage_a = _stage(tmp_path, {"a.txt": "writer a"}, "src2a")
        stage_b = _stage(tmp_path, {"b.txt": "writer b"}, "src2b")
        plan_a = root.plan(OID, stage_a, created="2024-02-01T00:00:00Z")
        plan_b = root.plan(OID, stage_b, created="2024-02-01T00:00:01Z")

        root.commit(fs, OID, plan_a, content=stage_a.content_source)
        with pytest.raises(StaleHeadError):
            root.commit(fs, OID, plan_b, content=stage_b.content_source)

        obj = root.get_object(OID)
        assert obj.head == VNum(2)
        _valid(fs, obj)

    def test_object_id_must_match_inventory(self, tmp_path: Path):
        fs, root = _root(tmp_path)
        root.commit(fs, OID, _stage(tmp_path, {"file.txt": "x"}))
        (tmp_path / "root" / OID).rename(tmp_path / "root" / "other")
        with pytest.raises(PlanMismatchError):
            root.plan("other", _stage(tmp_path, {"f": "y"}, "s2"))
