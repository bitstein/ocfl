"""Tests for OCFL Object write operations."""

from typing import TYPE_CHECKING

import pytest

from ocfl_lib.digest import DigestAlgorithm
from ocfl_lib.errors import StaleHeadError
from ocfl_lib.fs.local import LocalFS
from ocfl_lib.inventory import UserInfo
from ocfl_lib.object import Object
from ocfl_lib.spec import Spec, VNum
from ocfl_lib.update_plan import deserialize, revert_plan, serialize
from ocfl_lib.validation.object import validate_object
from ocfl_lib.validation.result import ValidationResult
from ocfl_lib.version_state import VersionState, scan_dir

if TYPE_CHECKING:
    from pathlib import Path


def _make_src_dir(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create a source directory with given files."""
    src = tmp_path / "src"
    src.mkdir()
    for name, content in files.items():
        p = src / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return src


class TestObjectUpdate:
    """Object.update() — full write path."""

    def test_create_new_object(self, tmp_path: Path):
        """Create a new v1 object from a staged directory."""
        src = _make_src_dir(tmp_path, {"file.txt": "hello"})
        obj_dir = tmp_path / "obj"
        obj_dir.mkdir()

        src_fs = LocalFS(src.parent)
        stage = scan_dir(src_fs, "src", DigestAlgorithm.SHA512)

        obj_fs = LocalFS(obj_dir)
        obj = Object(obj_fs, "")
        assert not obj.exists

        obj.update(
            obj_fs,
            stage,
            id="http://example.org/obj1",
            spec=Spec.V1_1,
            message="initial commit",
            user=UserInfo(name="Test User"),
            created="2024-01-01T00:00:00Z",
        )

        assert obj.exists
        assert obj.id == "http://example.org/obj1"
        assert obj.head == VNum(1)
        assert obj.spec == Spec.V1_1

        # Validate the created object
        result = ValidationResult()
        validate_object(obj_fs, "", result)
        assert result.is_valid, f"errors: {[e.message for e in result.errors]}"

    def test_create_object_reads_back_content(self, tmp_path: Path):
        """Content is accessible after creation."""
        src = _make_src_dir(tmp_path, {"file.txt": "hello world"})
        obj_dir = tmp_path / "obj"
        obj_dir.mkdir()

        src_fs = LocalFS(src.parent)
        stage = scan_dir(src_fs, "src", DigestAlgorithm.SHA512)

        obj_fs = LocalFS(obj_dir)
        obj = Object(obj_fs, "")
        obj.update(
            obj_fs,
            stage,
            id="test:1",
            spec=Spec.V1_1,
            created="2024-01-01T00:00:00Z",
        )

        with obj.open_logical_path(VNum(1), "file.txt") as f:
            assert f.read() == b"hello world"

    def test_add_version_to_existing(self, tmp_path: Path):
        """Add v2 to an existing object."""
        src1 = _make_src_dir(tmp_path, {"file.txt": "v1 content"})
        obj_dir = tmp_path / "obj"
        obj_dir.mkdir()

        src_fs = LocalFS(src1.parent)
        stage1 = scan_dir(src_fs, "src", DigestAlgorithm.SHA512)

        obj_fs = LocalFS(obj_dir)
        obj = Object(obj_fs, "")
        obj.update(
            obj_fs,
            stage1,
            id="test:1",
            spec=Spec.V1_1,
            created="2024-01-01T00:00:00Z",
        )

        # Create v2 with new content
        src2_dir = tmp_path / "src2"
        src2_dir.mkdir()
        (src2_dir / "file.txt").write_text("v2 content")
        (src2_dir / "new.txt").write_text("brand new")

        src2_fs = LocalFS(tmp_path)
        stage2 = scan_dir(src2_fs, "src2", DigestAlgorithm.SHA512)

        obj.update(
            obj_fs,
            stage2,
            message="second version",
            created="2024-06-01T00:00:00Z",
        )

        assert obj.head == VNum(2)
        assert len(obj.versions()) == 2

        result = ValidationResult()
        validate_object(obj_fs, "", result)
        assert result.is_valid, f"errors: {[e.message for e in result.errors]}"

    def test_forward_delta_dedup(self, tmp_path: Path):
        """Unchanged files reuse v1 content paths (no duplication)."""
        src1 = _make_src_dir(tmp_path, {"a.txt": "same", "b.txt": "old"})
        obj_dir = tmp_path / "obj"
        obj_dir.mkdir()

        src_fs = LocalFS(src1.parent)
        stage1 = scan_dir(src_fs, "src", DigestAlgorithm.SHA512)

        obj_fs = LocalFS(obj_dir)
        obj = Object(obj_fs, "")
        obj.update(
            obj_fs,
            stage1,
            id="test:1",
            spec=Spec.V1_1,
            created="2024-01-01T00:00:00Z",
        )

        # v2: a.txt unchanged, b.txt modified
        src2 = tmp_path / "src2"
        src2.mkdir()
        (src2 / "a.txt").write_text("same")
        (src2 / "b.txt").write_text("new")

        stage2 = scan_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)
        obj.update(obj_fs, stage2, created="2024-06-01T00:00:00Z")

        # v2 content dir should only have b.txt (a.txt deduplicated)
        inv = obj.inventory.inventory
        v2_content_paths = [
            p for d, ps in inv.manifest.items() for p in ps if p.startswith("v2/")
        ]
        assert len(v2_content_paths) == 1
        assert "b.txt" in v2_content_paths[0]

    def test_create_requires_id(self, tmp_path: Path):
        """Creating new object without id raises."""
        src = _make_src_dir(tmp_path, {"file.txt": "x"})
        obj_dir = tmp_path / "obj"
        obj_dir.mkdir()

        src_fs = LocalFS(src.parent)
        stage = scan_dir(src_fs, "src", DigestAlgorithm.SHA512)

        obj_fs = LocalFS(obj_dir)
        obj = Object(obj_fs, "")
        with pytest.raises(ValueError, match="id"):
            obj.update(obj_fs, stage, created="2024-01-01T00:00:00Z")

    def test_create_with_fixity(self, tmp_path: Path):
        """Fixity from stage is included in inventory."""
        src = _make_src_dir(tmp_path, {"file.txt": "hello"})
        obj_dir = tmp_path / "obj"
        obj_dir.mkdir()

        src_fs = LocalFS(src.parent)
        stage = scan_dir(
            src_fs, "src", DigestAlgorithm.SHA512, fixity_algs=[DigestAlgorithm.MD5]
        )
        assert stage.fixity_source is not None

        obj_fs = LocalFS(obj_dir)
        obj = Object(obj_fs, "")
        obj.update(
            obj_fs,
            stage,
            id="test:1",
            spec=Spec.V1_1,
            created="2024-01-01T00:00:00Z",
        )

        inv = obj.inventory.inventory
        assert inv.fixity is not None
        assert "md5" in inv.fixity

        result = ValidationResult()
        validate_object(obj_fs, "", result)
        assert result.is_valid

    def test_error_reverts_on_failure(self, tmp_path: Path):
        """On write failure, completed steps are reverted."""
        obj_dir = tmp_path / "obj"
        obj_dir.mkdir()

        # Create a stage with a content source that will fail
        class FailingContentSource:
            def get_content(self, digest: str) -> tuple:
                raise OSError("disk full")

        stage = VersionState(
            state={"abc123": ["file.txt"]},
            digest_algorithm=DigestAlgorithm.SHA512,
            content_source=FailingContentSource(),
        )

        obj_fs = LocalFS(obj_dir)
        obj = Object(obj_fs, "")
        with pytest.raises(OSError, match="disk full"):
            obj.update(
                obj_fs,
                stage,
                id="test:1",
                spec=Spec.V1_1,
                created="2024-01-01T00:00:00Z",
            )

        # Object dir should be cleaned up (v1 revert removes all)
        assert not obj_dir.exists() or not any(obj_dir.iterdir())

    def test_keyboard_interrupt_propagates_without_revert(self, tmp_path: Path):
        """KeyboardInterrupt propagates from update() without auto-revert.

        Library treats BaseException as out-of-band: callers wanting clean
        rollback on Ctrl-C must use the explicit plan/revert form.
        """
        obj_dir = tmp_path / "obj"
        obj_dir.mkdir()

        class InterruptingContentSource:
            def get_content(self, digest: str) -> tuple:
                raise KeyboardInterrupt

        stage = VersionState(
            state={"abc123": ["file.txt"]},
            digest_algorithm=DigestAlgorithm.SHA512,
            content_source=InterruptingContentSource(),
        )

        obj_fs = LocalFS(obj_dir)
        obj = Object(obj_fs, "")
        with pytest.raises(KeyboardInterrupt):
            obj.update(
                obj_fs,
                stage,
                id="test:1",
                spec=Spec.V1_1,
                created="2024-01-01T00:00:00Z",
            )

    def test_keyboard_interrupt_v2_preserves_root_inventory(self, tmp_path: Path):
        """KeyboardInterrupt during v2 → v1 root inventory still intact.

        Root inventory swap is the final step; KI before then leaves v1's
        root inventory untouched even though no auto-revert runs.
        """
        src = _make_src_dir(tmp_path, {"file.txt": "v1-content"})
        obj_dir = tmp_path / "obj"
        obj_dir.mkdir()

        src_fs = LocalFS(src.parent)
        stage = scan_dir(src_fs, "src", DigestAlgorithm.SHA512)

        obj_fs = LocalFS(obj_dir)
        obj = Object(obj_fs, "")
        obj.update(
            obj_fs,
            stage,
            id="test:1",
            spec=Spec.V1_1,
            created="2024-01-01T00:00:00Z",
        )

        v1_inv_bytes = (obj_dir / "inventory.json").read_bytes()
        v1_sidecar_bytes = (obj_dir / "inventory.json.sha512").read_bytes()

        class InterruptingContentSource:
            def get_content(self, digest: str) -> tuple:
                raise KeyboardInterrupt

        bad_stage = VersionState(
            state={"deadbeef": ["new.txt"]},
            digest_algorithm=DigestAlgorithm.SHA512,
            content_source=InterruptingContentSource(),
        )

        obj2 = Object(obj_fs, "")
        with pytest.raises(KeyboardInterrupt):
            obj2.update(
                obj_fs,
                bad_stage,
                message="v2",
                created="2024-01-02T00:00:00Z",
            )

        assert (obj_dir / "inventory.json").read_bytes() == v1_inv_bytes
        assert (obj_dir / "inventory.json.sha512").read_bytes() == v1_sidecar_bytes

    def test_update_caches_new_inventory(self, tmp_path: Path):
        """After update(), inventory property reflects new version."""
        src = _make_src_dir(tmp_path, {"file.txt": "hello"})
        obj_dir = tmp_path / "obj"
        obj_dir.mkdir()

        src_fs = LocalFS(src.parent)
        stage = scan_dir(src_fs, "src", DigestAlgorithm.SHA512)

        obj_fs = LocalFS(obj_dir)
        obj = Object(obj_fs, "")
        obj.update(
            obj_fs,
            stage,
            id="test:1",
            spec=Spec.V1_1,
            created="2024-01-01T00:00:00Z",
        )

        # inventory should be cached with new data
        assert obj.inventory.inventory.head == "v1"
        assert obj.inventory.inventory.id == "test:1"

    def test_multiple_files(self, tmp_path: Path):
        """Object with multiple files in nested dirs."""
        src = _make_src_dir(
            tmp_path,
            {
                "a.txt": "aaa",
                "dir/b.txt": "bbb",
                "dir/sub/c.txt": "ccc",
            },
        )
        obj_dir = tmp_path / "obj"
        obj_dir.mkdir()

        src_fs = LocalFS(src.parent)
        stage = scan_dir(src_fs, "src", DigestAlgorithm.SHA512)

        obj_fs = LocalFS(obj_dir)
        obj = Object(obj_fs, "")
        obj.update(
            obj_fs,
            stage,
            id="test:1",
            spec=Spec.V1_1,
            created="2024-01-01T00:00:00Z",
        )

        state = obj.version_info(VNum(1)).state
        all_paths = sorted(p for ps in state.values() for p in ps)
        assert all_paths == ["a.txt", "dir/b.txt", "dir/sub/c.txt"]

        result = ValidationResult()
        validate_object(obj_fs, "", result)
        assert result.is_valid

    def test_version_info_preserved(self, tmp_path: Path):
        """Message and user preserved in version info."""
        src = _make_src_dir(tmp_path, {"file.txt": "hello"})
        obj_dir = tmp_path / "obj"
        obj_dir.mkdir()

        src_fs = LocalFS(src.parent)
        stage = scan_dir(src_fs, "src", DigestAlgorithm.SHA512)
        user = UserInfo(name="Alice", address="mailto:alice@example.org")

        obj_fs = LocalFS(obj_dir)
        obj = Object(obj_fs, "")
        obj.update(
            obj_fs,
            stage,
            id="test:1",
            spec=Spec.V1_1,
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

    def test_update_fresh_instance_on_existing_object(self, tmp_path: Path):
        """Fresh Object instance on existing dir loads inventory before update."""
        src1 = _make_src_dir(tmp_path, {"file.txt": "v1"})
        obj_dir = tmp_path / "obj"
        obj_dir.mkdir()

        src_fs = LocalFS(src1.parent)
        stage1 = scan_dir(src_fs, "src", DigestAlgorithm.SHA512)

        obj_fs = LocalFS(obj_dir)
        obj1 = Object(obj_fs, "")
        obj1.update(
            obj_fs,
            stage1,
            id="test:1",
            spec=Spec.V1_1,
            created="2024-01-01T00:00:00Z",
        )

        # Create entirely new Object instance (inventory not cached)
        obj2 = Object(obj_fs, "")
        assert obj2.exists

        src2 = tmp_path / "src2"
        src2.mkdir()
        (src2 / "file.txt").write_text("v2")
        stage2 = scan_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)

        obj2.update(obj_fs, stage2, created="2024-06-01T00:00:00Z")
        assert obj2.head == VNum(2)  # NOT v1

        result = ValidationResult()
        validate_object(obj_fs, "", result)
        assert result.is_valid


class TestTwoStepWrite:
    """Object.new_update_plan() / apply_update_plan()."""

    def test_create_via_two_step(self, tmp_path: Path):
        """Create object via plan + apply."""
        src = _make_src_dir(tmp_path, {"file.txt": "hello"})
        obj_dir = tmp_path / "obj"
        obj_dir.mkdir()

        src_fs = LocalFS(src.parent)
        stage = scan_dir(src_fs, "src", DigestAlgorithm.SHA512)

        obj_fs = LocalFS(obj_dir)
        obj = Object(obj_fs, "")

        plan = obj.new_update_plan(
            stage,
            id="test:1",
            spec=Spec.V1_1,
            created="2024-01-01T00:00:00Z",
        )
        # Plan created but object not yet written
        assert not obj.exists

        obj.apply_update_plan(obj_fs, plan, stage.content_source)
        assert obj.exists
        assert obj.head == VNum(1)

        result = ValidationResult()
        validate_object(obj_fs, "", result)
        assert result.is_valid

    def test_plan_serializable(self, tmp_path: Path):
        """Plan can be serialized, deserialized, and applied."""
        src = _make_src_dir(tmp_path, {"file.txt": "hello"})
        obj_dir = tmp_path / "obj"
        obj_dir.mkdir()

        src_fs = LocalFS(src.parent)
        stage = scan_dir(src_fs, "src", DigestAlgorithm.SHA512)

        obj_fs = LocalFS(obj_dir)
        obj = Object(obj_fs, "")

        plan = obj.new_update_plan(
            stage,
            id="test:1",
            spec=Spec.V1_1,
            created="2024-01-01T00:00:00Z",
        )

        # Serialize and deserialize
        data = serialize(plan)
        plan2 = deserialize(data)

        # Apply deserialized plan
        obj.apply_update_plan(obj_fs, plan2, stage.content_source)
        assert obj.exists

        result = ValidationResult()
        validate_object(obj_fs, "", result)
        assert result.is_valid

    def test_no_auto_revert(self, tmp_path: Path):
        """apply_update_plan does NOT auto-revert — caller decides."""
        obj_dir = tmp_path / "obj"
        obj_dir.mkdir()

        class FailingSource:
            def get_content(self, digest: str) -> tuple:
                raise OSError("fail")

        stage = VersionState(
            state={"abc123": ["file.txt"]},
            digest_algorithm=DigestAlgorithm.SHA512,
            content_source=FailingSource(),
        )

        obj_fs = LocalFS(obj_dir)
        obj = Object(obj_fs, "")

        plan = obj.new_update_plan(
            stage,
            id="test:1",
            spec=Spec.V1_1,
            created="2024-01-01T00:00:00Z",
        )

        with pytest.raises(OSError, match="fail"):
            obj.apply_update_plan(obj_fs, plan, stage.content_source)

        # Some steps completed (declaration, version-dir) but NOT reverted
        completed = [s for s in plan.steps if s.completed]
        assert len(completed) > 0

    def test_v2_via_two_step(self, tmp_path: Path):
        """Add v2 via two-step API on existing object."""
        src1 = _make_src_dir(tmp_path, {"file.txt": "v1"})
        obj_dir = tmp_path / "obj"
        obj_dir.mkdir()

        src_fs = LocalFS(src1.parent)
        stage1 = scan_dir(src_fs, "src", DigestAlgorithm.SHA512)

        obj_fs = LocalFS(obj_dir)
        obj = Object(obj_fs, "")
        obj.update(
            obj_fs,
            stage1,
            id="test:1",
            spec=Spec.V1_1,
            created="2024-01-01T00:00:00Z",
        )

        # v2 via two-step
        src2 = tmp_path / "src2"
        src2.mkdir()
        (src2 / "file.txt").write_text("v2")

        stage2 = scan_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)
        plan = obj.new_update_plan(stage2, created="2024-06-01T00:00:00Z")
        obj.apply_update_plan(obj_fs, plan, stage2.content_source)

        assert obj.head == VNum(2)
        result = ValidationResult()
        validate_object(obj_fs, "", result)
        assert result.is_valid

    def test_independently_built_stale_plan_cannot_publish(self, tmp_path: Path):
        """Two v2 plans built from the same v1 head cannot both publish."""
        src1 = _make_src_dir(tmp_path, {"base.txt": "v1"})
        obj_dir = tmp_path / "obj"
        obj_dir.mkdir()

        obj_fs = LocalFS(obj_dir)
        obj = Object(obj_fs, "")
        obj.update(
            obj_fs,
            scan_dir(LocalFS(src1.parent), "src", DigestAlgorithm.SHA512),
            id="test:stale",
            spec=Spec.V1_1,
            created="2024-01-01T00:00:00Z",
        )

        src2a = tmp_path / "src2a"
        src2a.mkdir()
        (src2a / "a.txt").write_text("writer a")
        src2b = tmp_path / "src2b"
        src2b.mkdir()
        (src2b / "b.txt").write_text("writer b")

        stage_a = scan_dir(LocalFS(tmp_path), "src2a", DigestAlgorithm.SHA512)
        stage_b = scan_dir(LocalFS(tmp_path), "src2b", DigestAlgorithm.SHA512)

        obj_a = Object(obj_fs, "")
        obj_b = Object(obj_fs, "")
        plan_a = obj_a.new_update_plan(stage_a, created="2024-02-01T00:00:00Z")
        plan_b = obj_b.new_update_plan(stage_b, created="2024-02-01T00:00:01Z")

        obj_a.apply_update_plan(obj_fs, plan_a, stage_a.content_source)

        with pytest.raises(StaleHeadError):
            obj_b.apply_update_plan(obj_fs, plan_b, stage_b.content_source)

        assert Object(obj_fs, "").head == VNum(2)
        result = ValidationResult()
        validate_object(obj_fs, "", result)
        assert result.is_valid, f"errors: {[e.message for e in result.errors]}"

    def test_stale_plan_revert_removes_copied_content(self, tmp_path: Path):
        """A stale plan can be reverted without leaving unmanifested content."""
        src1 = _make_src_dir(tmp_path, {"base.txt": "v1"})
        obj_dir = tmp_path / "obj"
        obj_dir.mkdir()

        obj_fs = LocalFS(obj_dir)
        obj = Object(obj_fs, "")
        obj.update(
            obj_fs,
            scan_dir(LocalFS(src1.parent), "src", DigestAlgorithm.SHA512),
            id="test:stale-revert",
            spec=Spec.V1_1,
            created="2024-01-01T00:00:00Z",
        )

        src2 = tmp_path / "src2"
        src2.mkdir()
        (src2 / "new.txt").write_text("new content")
        stage = scan_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)

        plan = Object(obj_fs, "").new_update_plan(stage, created="2024-02-01T00:00:00Z")
        (obj_dir / "inventory.json.sha512").write_text("0" * 128 + " inventory.json\n")

        with pytest.raises(StaleHeadError):
            Object(obj_fs, "").apply_update_plan(obj_fs, plan, stage.content_source)

        revert_plan(plan)

        assert not (obj_dir / "v2").exists()
        (obj_dir / "inventory.json.sha512").write_text(obj.inventory.sidecar_content)
        result = ValidationResult()
        validate_object(obj_fs, "", result)
        assert result.is_valid, f"errors: {[e.message for e in result.errors]}"
