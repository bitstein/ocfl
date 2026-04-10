"""Tests for update_plan module."""

import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ocfl_lib.digest import DigestAlgorithm
from ocfl_lib.errors import RevertCompletedError
from ocfl_lib.fs.local import LocalFS
from ocfl_lib.inventory import Inventory, StoredInventory, VersionInfo

if TYPE_CHECKING:
    from ocfl_lib.fs.protocols import FS
from ocfl_lib.inventory_builder import InventoryBuilder
from ocfl_lib.update_plan import (
    PlanStep,
    RevertError,
    UpdatePlan,
    deserialize,
    execute_plan,
    prepare_steps,
    revert_plan,
    serialize,
)


def test_revert_plan_refuses_completed():
    inv = _make_stored_inventory()
    plan = UpdatePlan(
        old_inv=None,
        new_inv=inv,
        steps=[
            PlanStep(name="a", completed=True),
            PlanStep(name="b", completed=True),
        ],
    )
    with pytest.raises(RevertCompletedError):
        revert_plan(plan)


def _make_stored_inventory(
    *, obj_id: str = "test:obj1", head: str = "v1"
) -> StoredInventory:
    """Create a minimal StoredInventory for testing."""
    inv = Inventory(
        id=obj_id,
        type="https://ocfl.io/1.1/spec/#inventory",
        digest_algorithm="sha512",
        head=head,
        manifest={
            "abc123": ["v1/content/file.txt"],
        },
        versions={
            "v1": VersionInfo(
                created="2024-01-01T00:00:00Z",
                state={"abc123": ["file.txt"]},
            ),
        },
    )
    return StoredInventory.from_inventory(inv, DigestAlgorithm.SHA512)


class TestPlanStep:
    def test_construction_defaults(self):
        step = PlanStep(name="test-step")
        assert step.name == "test-step"
        assert step.content_digest == ""
        assert step.is_async is False
        assert step.completed is False
        assert step.size == 0
        assert step.error == ""
        assert step.revert_error == ""
        assert step.run is None
        assert step.revert is None

    def test_construction_full(self):
        def run_fn() -> int:
            return 0

        def revert_fn() -> None:
            pass

        step = PlanStep(
            name="copy-file",
            content_digest="abc123",
            is_async=True,
            completed=False,
            size=1024,
            run=run_fn,
            revert=revert_fn,
        )
        assert step.name == "copy-file"
        assert step.content_digest == "abc123"
        assert step.is_async is True
        assert step.size == 1024
        assert step.run is run_fn
        assert step.revert is revert_fn


class TestUpdatePlan:
    def test_construction(self):
        new_inv = _make_stored_inventory()
        plan = UpdatePlan(
            steps=[PlanStep(name="step1")],
            new_inv=new_inv,
            old_inv=None,
        )
        assert len(plan.steps) == 1
        assert plan.new_inv is new_inv
        assert plan.old_inv is None

    def test_construction_with_old_inv(self):
        old_inv = _make_stored_inventory(obj_id="test:old")
        new_inv = _make_stored_inventory(obj_id="test:new", head="v2")
        plan = UpdatePlan(
            steps=[PlanStep(name="s1"), PlanStep(name="s2")],
            new_inv=new_inv,
            old_inv=old_inv,
        )
        assert plan.old_inv is old_inv
        assert plan.new_inv is new_inv
        assert len(plan.steps) == 2


class TestSerialization:
    def test_round_trip_no_old_inv(self):
        new_inv = _make_stored_inventory()
        steps = [
            PlanStep(name="object-root"),
            PlanStep(name="declaration"),
            PlanStep(name="copy-file", content_digest="abc123", is_async=True),
        ]
        plan = UpdatePlan(steps=steps, new_inv=new_inv, old_inv=None)

        data = serialize(plan)
        assert isinstance(data, bytes)

        restored = deserialize(data)
        assert len(restored.steps) == 3
        assert restored.steps[0].name == "object-root"
        assert restored.steps[2].content_digest == "abc123"
        assert restored.steps[2].is_async is True
        # Callables not restored
        assert restored.steps[0].run is None
        assert restored.steps[0].revert is None
        assert restored.old_inv is None

    def test_round_trip_with_old_inv(self):
        old_inv = _make_stored_inventory(obj_id="test:old")
        new_inv = _make_stored_inventory(obj_id="test:new")
        plan = UpdatePlan(
            steps=[PlanStep(name="s1", completed=True, size=512)],
            new_inv=new_inv,
            old_inv=old_inv,
        )

        data = serialize(plan)
        restored = deserialize(data)
        assert restored.old_inv is not None
        assert restored.old_inv.inventory.id == "test:old"
        assert restored.new_inv.inventory.id == "test:new"
        assert restored.steps[0].completed is True
        assert restored.steps[0].size == 512

    def test_inventory_bytes_preserved(self):
        """Exact raw_bytes must survive serialization round-trip."""
        new_inv = _make_stored_inventory()
        plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)

        data = serialize(plan)
        restored = deserialize(data)
        assert restored.new_inv.raw_bytes == new_inv.raw_bytes
        assert restored.new_inv.digest == new_inv.digest

    def test_inventory_bytes_preserved_with_old(self):
        old_inv = _make_stored_inventory(obj_id="test:old")
        new_inv = _make_stored_inventory(obj_id="test:new")
        plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=old_inv)

        data = serialize(plan)
        restored = deserialize(data)
        assert restored.old_inv is not None
        assert restored.old_inv.raw_bytes == old_inv.raw_bytes
        assert restored.new_inv.raw_bytes == new_inv.raw_bytes

    def test_revert_inv_round_trip(self):
        """revert_inv survives serialization round-trip."""
        revert_inv = _make_stored_inventory(obj_id="test:revert")
        old_inv = _make_stored_inventory(obj_id="test:migrated")
        new_inv = _make_stored_inventory(obj_id="test:new")
        plan = UpdatePlan(
            steps=[],
            new_inv=new_inv,
            old_inv=old_inv,
            revert_inv=revert_inv,
        )

        data = serialize(plan)
        restored = deserialize(data)
        assert restored.revert_inv is not None
        assert restored.revert_inv.raw_bytes == revert_inv.raw_bytes
        assert restored.revert_inv.digest == revert_inv.digest

    def test_revert_inv_none_by_default(self):
        """revert_inv is None when not set."""
        plan = UpdatePlan(
            steps=[],
            new_inv=_make_stored_inventory(),
            old_inv=None,
        )
        data = serialize(plan)
        restored = deserialize(data)
        assert restored.revert_inv is None

    def test_step_error_fields_preserved(self):
        steps = [
            PlanStep(name="failed", error="disk full", completed=True),
            PlanStep(name="reverted", revert_error="cleanup failed"),
        ]
        plan = UpdatePlan(
            steps=steps,
            new_inv=_make_stored_inventory(),
            old_inv=None,
        )
        data = serialize(plan)
        restored = deserialize(data)
        assert restored.steps[0].error == "disk full"
        assert restored.steps[1].revert_error == "cleanup failed"


class _DictContentSource:
    """ContentSource backed by a dict of digest → bytes in a temp dir."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self._tmpobj = tempfile.TemporaryDirectory()
        self._tmpdir = Path(self._tmpobj.name)
        self._fs = LocalFS(self._tmpdir)
        for d, data in files.items():
            (self._tmpdir / d).write_bytes(data)

    def get_content(self, digest: str) -> tuple[FS, str]:
        return (self._fs, digest)


class _FailingContentSource:
    """ContentSource that raises for designated digests, otherwise serves bytes."""

    def __init__(self, files: dict[str, bytes], fail_digests: set[str]) -> None:
        self._tmpobj = tempfile.TemporaryDirectory()
        self._tmpdir = Path(self._tmpobj.name)
        self._fs = LocalFS(self._tmpdir)
        for d, data in files.items():
            (self._tmpdir / d).write_bytes(data)
        self._fail = fail_digests

    def get_content(self, digest: str) -> tuple[FS, str]:
        if digest in self._fail:
            raise RuntimeError(f"injected failure for {digest}")
        return (self._fs, digest)


def _build_v1_inventory(
    *,
    obj_id: str = "test:obj1",
    files: dict[str, str] | None = None,
) -> StoredInventory:
    """Build a v1 StoredInventory with given logical files.

    files: {logical_path: digest}
    """
    if files is None:
        files = {"file.txt": "aabbcc"}
    # Build state: digest → [logical_paths]
    state: dict[str, list[str]] = {}
    for path, digest in files.items():
        state.setdefault(digest, []).append(path)

    inv = (
        InventoryBuilder()
        .id(obj_id)
        .add_version(state, created="2024-01-01T00:00:00Z")
        .build()
    )
    return StoredInventory.from_inventory(inv, DigestAlgorithm.SHA512)


def _build_v2_inventory(
    old_inv: StoredInventory,
    *,
    new_files: dict[str, str] | None = None,
) -> StoredInventory:
    """Build a v2 StoredInventory adding new files."""
    if new_files is None:
        new_files = {"file.txt": "aabbcc", "new.txt": "ddeeff"}
    state: dict[str, list[str]] = {}
    for path, digest in new_files.items():
        state.setdefault(digest, []).append(path)

    inv = (
        InventoryBuilder(old_inv.inventory)
        .add_version(state, created="2024-02-01T00:00:00Z")
        .build()
    )
    return StoredInventory.from_inventory(inv, DigestAlgorithm.SHA512)


class TestPrepareSteps:
    def test_new_object_step_names(self, tmp_path: Path):
        """New object (v1) should have: object-root, declaration, version-dir,
        content copies, version-inv, version-sidecar, root-inv, root-sidecar."""
        new_inv = _build_v1_inventory()
        plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)
        fsys = LocalFS(tmp_path)
        source = _DictContentSource({"aabbcc": b"hello"})

        prepare_steps(plan, fsys, "obj1", source)

        names = [s.name for s in plan.steps]
        assert names[0] == "object-root"
        assert names[1] == "declaration"
        assert names[2] == "version-dir"
        # Content copies
        content_steps = [s for s in plan.steps if s.name == "content-copy"]
        assert len(content_steps) == 1
        assert content_steps[0].is_async is True
        assert content_steps[0].content_digest == "aabbcc"
        # Inventory steps at end
        assert names[-4] == "version-inv"
        assert names[-3] == "version-sidecar"
        assert names[-2] == "root-inv"
        assert names[-1] == "root-sidecar"

    def test_new_object_step_count(self, tmp_path: Path):
        """v1 with 1 file: 8 steps total."""
        new_inv = _build_v1_inventory()
        plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)
        fsys = LocalFS(tmp_path)
        source = _DictContentSource({"aabbcc": b"hello"})

        prepare_steps(plan, fsys, "obj1", source)
        assert len(plan.steps) == 8

    def test_update_object_no_object_root_step(self, tmp_path: Path):
        """Updating existing object (v2) should NOT have object-root step."""
        old_inv = _build_v1_inventory()
        new_inv = _build_v2_inventory(old_inv)
        plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=old_inv)
        fsys = LocalFS(tmp_path)
        source = _DictContentSource({"ddeeff": b"world"})

        prepare_steps(plan, fsys, "obj1", source)

        names = [s.name for s in plan.steps]
        assert "object-root" not in names
        assert names[0] == "declaration"

    def test_update_only_new_content_copied(self, tmp_path: Path):
        """Only digests not in old manifest get content-copy steps."""
        old_inv = _build_v1_inventory(files={"file.txt": "aabbcc"})
        new_inv = _build_v2_inventory(
            old_inv, new_files={"file.txt": "aabbcc", "new.txt": "ddeeff"}
        )
        plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=old_inv)
        fsys = LocalFS(tmp_path)
        source = _DictContentSource({"ddeeff": b"world"})

        prepare_steps(plan, fsys, "obj1", source)

        copies = [s for s in plan.steps if s.name == "content-copy"]
        assert len(copies) == 1
        assert copies[0].content_digest == "ddeeff"

    def test_all_steps_have_callables(self, tmp_path: Path):
        """After prepare_steps, all steps must have run and revert callables."""
        new_inv = _build_v1_inventory()
        plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)
        fsys = LocalFS(tmp_path)
        source = _DictContentSource({"aabbcc": b"hello"})

        prepare_steps(plan, fsys, "obj1", source)

        for step in plan.steps:
            assert step.run is not None, f"step {step.name!r} missing run"
            assert step.revert is not None, f"step {step.name!r} missing revert"

    def test_callables_work_new_object(self, tmp_path: Path):
        """Running all step callables in order should produce valid files."""
        new_inv = _build_v1_inventory()
        plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)
        fsys = LocalFS(tmp_path)
        source = _DictContentSource({"aabbcc": b"hello"})

        prepare_steps(plan, fsys, "obj1", source)

        # Execute all steps in order
        for step in plan.steps:
            assert step.run is not None
            step.run()

        # Verify object structure exists
        obj_dir = tmp_path / "obj1"
        assert obj_dir.is_dir()
        assert (obj_dir / "0=ocfl_object_1.1").is_file()
        assert (obj_dir / "v1" / "content" / "file.txt").is_file()
        assert (obj_dir / "v1" / "content" / "file.txt").read_bytes() == b"hello"
        assert (obj_dir / "v1" / "inventory.json").is_file()
        assert (obj_dir / "inventory.json").is_file()
        # Root and version inventory should be identical
        assert (obj_dir / "inventory.json").read_bytes() == new_inv.raw_bytes
        assert (obj_dir / "v1" / "inventory.json").read_bytes() == new_inv.raw_bytes

    def test_multiple_files(self, tmp_path: Path):
        """Multiple files produce multiple content-copy steps."""
        files = {"a.txt": "digest1", "b.txt": "digest2", "c.txt": "digest3"}
        new_inv = _build_v1_inventory(files=files)
        plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)
        fsys = LocalFS(tmp_path)
        source = _DictContentSource(
            {
                "digest1": b"aaa",
                "digest2": b"bbb",
                "digest3": b"ccc",
            }
        )

        prepare_steps(plan, fsys, "obj1", source)

        copies = [s for s in plan.steps if s.name == "content-copy"]
        assert len(copies) == 3
        assert all(s.is_async for s in copies)


class TestExecutePlan:
    def test_creates_valid_object(self, tmp_path: Path):
        """execute_plan on empty dir should produce a valid OCFL object."""
        new_inv = _build_v1_inventory()
        plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)
        fsys = LocalFS(tmp_path)
        source = _DictContentSource({"aabbcc": b"hello"})

        prepare_steps(plan, fsys, "obj1", source)
        execute_plan(plan)

        # Verify on-disk structure
        obj_dir = tmp_path / "obj1"
        assert obj_dir.is_dir()
        assert (obj_dir / "0=ocfl_object_1.1").is_file()
        assert (obj_dir / "inventory.json").is_file()
        assert (obj_dir / "v1" / "content" / "file.txt").read_bytes() == b"hello"
        # Root and version inventories match
        assert (obj_dir / "inventory.json").read_bytes() == new_inv.raw_bytes
        assert (obj_dir / "v1" / "inventory.json").read_bytes() == new_inv.raw_bytes
        # Sidecar exists
        assert (obj_dir / new_inv.sidecar_filename).is_file()
        assert (obj_dir / "v1" / new_inv.sidecar_filename).is_file()

    def test_all_steps_marked_completed(self, tmp_path: Path):
        """After execution, all steps should be marked completed."""
        new_inv = _build_v1_inventory()
        plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)
        fsys = LocalFS(tmp_path)
        source = _DictContentSource({"aabbcc": b"hello"})

        prepare_steps(plan, fsys, "obj1", source)
        execute_plan(plan)

        for step in plan.steps:
            assert step.completed, f"step {step.name!r} not completed"

    def test_skips_completed_steps(self, tmp_path: Path):
        """Steps already marked completed should be skipped."""
        new_inv = _build_v1_inventory()
        plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)
        fsys = LocalFS(tmp_path)
        source = _DictContentSource({"aabbcc": b"hello"})

        prepare_steps(plan, fsys, "obj1", source)

        # Mark first few steps completed and run them manually
        for step in plan.steps[:3]:
            assert step.run is not None
            step.run()
            step.completed = True

        # Replace run on completed steps with a bomb
        for step in plan.steps[:3]:

            def _bomb() -> int:
                raise AssertionError("should not be called")

            step.run = _bomb

        # execute_plan should skip those and finish the rest
        execute_plan(plan)

        for step in plan.steps:
            assert step.completed

    def test_step_failure_sets_error(self, tmp_path: Path):
        """If a step fails, its error field is set and exception raised."""
        new_inv = _build_v1_inventory()
        plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)
        fsys = LocalFS(tmp_path)
        source = _DictContentSource({"aabbcc": b"hello"})

        prepare_steps(plan, fsys, "obj1", source)

        # Replace a step's run with a failing callable
        plan.steps[3].run = _failing_step

        with pytest.raises(RuntimeError, match="disk full"):
            execute_plan(plan)

        assert plan.steps[3].error == "disk full"
        # Steps before the failure should be completed
        for step in plan.steps[:3]:
            assert step.completed

    def test_update_existing_object(self, tmp_path: Path):
        """Execute plan for v2 on an existing v1 object."""
        # First create v1
        old_inv = _build_v1_inventory()
        plan1 = UpdatePlan(steps=[], new_inv=old_inv, old_inv=None)
        fsys = LocalFS(tmp_path)
        source1 = _DictContentSource({"aabbcc": b"hello"})
        prepare_steps(plan1, fsys, "obj1", source1)
        execute_plan(plan1)

        # Now create v2
        new_inv = _build_v2_inventory(old_inv)
        plan2 = UpdatePlan(steps=[], new_inv=new_inv, old_inv=old_inv)
        source2 = _DictContentSource({"ddeeff": b"world"})
        prepare_steps(plan2, fsys, "obj1", source2)
        execute_plan(plan2)

        # v2 content exists
        assert (
            tmp_path / "obj1" / "v2" / "content" / "new.txt"
        ).read_bytes() == b"world"
        # Root inventory updated
        assert (tmp_path / "obj1" / "inventory.json").read_bytes() == new_inv.raw_bytes

    def test_concurrent_content_copies(self, tmp_path: Path):
        """Multiple content copies should run concurrently with max_workers."""
        files = {
            "a.txt": "d1",
            "b.txt": "d2",
            "c.txt": "d3",
        }
        new_inv = _build_v1_inventory(files=files)
        plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)
        fsys = LocalFS(tmp_path)
        source = _DictContentSource(
            {
                "d1": b"aaa",
                "d2": b"bbb",
                "d3": b"ccc",
            }
        )

        prepare_steps(plan, fsys, "obj1", source)
        execute_plan(plan, max_workers=2)

        # All files exist
        assert (tmp_path / "obj1" / "v1" / "content" / "a.txt").read_bytes() == b"aaa"
        assert (tmp_path / "obj1" / "v1" / "content" / "b.txt").read_bytes() == b"bbb"
        assert (tmp_path / "obj1" / "v1" / "content" / "c.txt").read_bytes() == b"ccc"
        for step in plan.steps:
            assert step.completed


def _failing_step() -> int:
    raise RuntimeError("disk full")


class TestConcurrentFailureModes:
    """§4.3 — failure modes during parallel content copies."""

    def test_async_failure_mid_batch_reverts_siblings(self, tmp_path: Path):
        """One copy fails while siblings succeed: full revert leaves no orphans."""
        files = {f"f{i}.txt": f"d{i}" for i in range(6)}
        new_inv = _build_v1_inventory(files=files)
        plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)
        fsys = LocalFS(tmp_path)
        source = _FailingContentSource(
            {f"d{i}": f"x{i}".encode() for i in range(6)},
            fail_digests={"d3"},
        )
        prepare_steps(plan, fsys, "obj1", source)

        with pytest.raises(RuntimeError, match="injected failure for d3"):
            execute_plan(plan, max_workers=4)

        # Failed step recorded
        failed = [s for s in plan.steps if s.error]
        assert any("d3" in s.error for s in failed)

        # Revert: object exists with partial content
        revert_plan(plan)
        assert not (tmp_path / "obj1").exists()

    def test_two_async_failures_fail_fast(self, tmp_path: Path):
        """First failure cancels pending work; at least one error recorded."""
        files = {f"f{i}.txt": f"d{i}" for i in range(8)}
        new_inv = _build_v1_inventory(files=files)
        plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)
        fsys = LocalFS(tmp_path)
        source = _FailingContentSource(
            {f"d{i}": f"x{i}".encode() for i in range(8)},
            fail_digests={"d2", "d5"},
        )
        prepare_steps(plan, fsys, "obj1", source)

        with pytest.raises(RuntimeError, match="injected failure"):
            execute_plan(plan, max_workers=4)

        errored_digests = {
            s.content_digest for s in plan.steps if s.error and s.content_digest
        }
        assert errored_digests & {"d2", "d5"}

        revert_plan(plan)
        assert not (tmp_path / "obj1").exists()

    def test_sync_failure_after_async_success_reverts_content(self, tmp_path: Path):
        """A post-async sync step (e.g. inventory write) fails after content
        copies completed. Revert must clean up the copied content."""
        files = {f"f{i}.txt": f"d{i}" for i in range(4)}
        new_inv = _build_v1_inventory(files=files)
        plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)
        fsys = LocalFS(tmp_path)
        source = _DictContentSource({f"d{i}": f"x{i}".encode() for i in range(4)})
        prepare_steps(plan, fsys, "obj1", source)

        # Inject failure into the post-async version-inv step
        vinv_step = next(s for s in plan.steps if s.name == "version-inv")
        vinv_step.run = _failing_step

        with pytest.raises(RuntimeError, match="disk full"):
            execute_plan(plan)

        # Content was copied
        assert any(s.completed and s.name == "content-copy" for s in plan.steps)
        # Revert removes everything
        revert_plan(plan)
        assert not (tmp_path / "obj1").exists()


class TestRevertPlan:
    def test_revert_refuses_fully_completed_plan(self, tmp_path: Path):
        """Reverting a fully-executed plan is refused (no undo of success)."""
        new_inv = _build_v1_inventory()
        plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)
        fsys = LocalFS(tmp_path)
        source = _DictContentSource({"aabbcc": b"hello"})

        prepare_steps(plan, fsys, "obj1", source)
        execute_plan(plan)

        with pytest.raises(RevertCompletedError):
            revert_plan(plan)

        # Object remains intact
        assert (tmp_path / "obj1").is_dir()

    def test_revert_partial_execution(self, tmp_path: Path):
        """Reverting a partially executed plan cleans up completed steps."""
        new_inv = _build_v1_inventory()
        plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)
        fsys = LocalFS(tmp_path)
        source = _DictContentSource({"aabbcc": b"hello"})

        prepare_steps(plan, fsys, "obj1", source)

        # Execute only first 3 steps (object-root, declaration, version-dir)
        for step in plan.steps[:3]:
            assert step.run is not None
            step.run()
            step.completed = True

        assert (tmp_path / "obj1").is_dir()

        revert_plan(plan)

        # Object removed
        assert not (tmp_path / "obj1").exists()

    def test_revert_only_completed_steps(self, tmp_path: Path):
        """Only completed steps should be reverted."""
        new_inv = _build_v1_inventory()
        plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)
        fsys = LocalFS(tmp_path)
        source = _DictContentSource({"aabbcc": b"hello"})

        prepare_steps(plan, fsys, "obj1", source)

        # Don't execute anything
        revert_plan(plan)

        # Nothing to revert — no error, no object dir
        assert not (tmp_path / "obj1").exists()

    def test_revert_continues_on_error(self, tmp_path: Path):
        """Revert should continue past a failing step, then raise RevertError
        listing the failures so the caller knows cleanup was incomplete."""
        new_inv = _build_v1_inventory()
        plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)
        fsys = LocalFS(tmp_path)
        source = _DictContentSource({"aabbcc": b"hello"})

        prepare_steps(plan, fsys, "obj1", source)
        execute_plan(plan)

        # Replace a middle step's revert with a failing callable
        def _bad_revert() -> None:
            raise RuntimeError("cleanup failed")

        plan.steps[3].revert = _bad_revert
        # Mark final step incomplete to bypass the all-completed guard
        plan.steps[-1].completed = False

        with pytest.raises(RevertError) as exc_info:
            revert_plan(plan)

        assert plan.steps[3].revert_error == "cleanup failed"
        assert ("content-copy", "cleanup failed") in exc_info.value.failures or any(
            err == "cleanup failed" for _, err in exc_info.value.failures
        )
        # Other steps were still reverted (object-root revert removed everything)
        assert not (tmp_path / "obj1").exists()

    def test_revert_v2_partial_preserves_v1(self, tmp_path: Path):
        """Partial v2 → revert leaves v1 intact (cleanup-after-failure)."""
        old_inv = _build_v1_inventory()
        plan1 = UpdatePlan(steps=[], new_inv=old_inv, old_inv=None)
        fsys = LocalFS(tmp_path)
        source1 = _DictContentSource({"aabbcc": b"hello"})
        prepare_steps(plan1, fsys, "obj1", source1)
        execute_plan(plan1)

        new_inv = _build_v2_inventory(old_inv)
        plan2 = UpdatePlan(steps=[], new_inv=new_inv, old_inv=old_inv)
        source2 = _DictContentSource({"ddeeff": b"world"})
        prepare_steps(plan2, fsys, "obj1", source2)
        execute_plan(plan2)

        assert (tmp_path / "obj1" / "v2").is_dir()

        # Mark one step incomplete to model interrupted execution
        plan2.steps[-1].completed = False
        revert_plan(plan2)

        assert not (tmp_path / "obj1" / "v2").exists()
        assert (tmp_path / "obj1" / "v1" / "content" / "file.txt").is_file()
        assert (tmp_path / "obj1" / "0=ocfl_object_1.1").is_file()
        assert (tmp_path / "obj1" / "inventory.json").read_bytes() == old_inv.raw_bytes
        assert (tmp_path / "obj1" / old_inv.sidecar_filename).is_file()


class TestPlanResume:
    """prepare_steps preserves completed state on resume."""

    def test_resume_skips_completed_steps(self, tmp_path: Path):
        """Serialize mid-execution plan, deserialize, prepare, execute
        → only incomplete steps run."""
        new_inv = _build_v1_inventory()
        plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)
        fsys = LocalFS(tmp_path)
        source = _DictContentSource({"aabbcc": b"hello"})

        prepare_steps(plan, fsys, "obj1", source)

        # Execute first 3 steps (object-root, declaration, version-dir)
        for step in plan.steps[:3]:
            assert step.run is not None
            step.run()
            step.completed = True

        # Serialize and deserialize (simulates process restart)
        data = serialize(plan)
        restored = deserialize(data)

        # Re-prepare with callables
        prepare_steps(restored, fsys, "obj1", source)

        # Completed flags preserved
        for step in restored.steps[:3]:
            assert step.completed

        # Execute remaining
        execute_plan(restored)

        # All steps completed, object valid on disk
        for step in restored.steps:
            assert step.completed
        obj_dir = tmp_path / "obj1"
        assert (obj_dir / "inventory.json").read_bytes() == new_inv.raw_bytes
        assert (obj_dir / "v1" / "content" / "file.txt").read_bytes() == b"hello"

    def test_resume_mismatch_raises(self, tmp_path: Path):
        """prepare_steps raises if existing steps don't match new steps."""
        inv1 = _build_v1_inventory(files={"a.txt": "d1"})
        inv2 = _build_v1_inventory(files={"a.txt": "d1", "b.txt": "d2"})

        # Build plan with inv1's steps
        plan = UpdatePlan(steps=[], new_inv=inv1, old_inv=None)
        fsys = LocalFS(tmp_path)
        source = _DictContentSource({"d1": b"aaa", "d2": b"bbb"})
        prepare_steps(plan, fsys, "obj1", source)

        # Swap inventory to inv2 (different step count)
        plan.new_inv = inv2

        with pytest.raises(ValueError, match="mismatch"):
            prepare_steps(plan, fsys, "obj1", source)

    def test_resume_digest_mismatch_raises(self, tmp_path: Path):
        """prepare_steps raises if content digests don't match."""
        inv1 = _build_v1_inventory(files={"a.txt": "d1"})
        inv2 = _build_v1_inventory(files={"a.txt": "d2"})

        plan = UpdatePlan(steps=[], new_inv=inv1, old_inv=None)
        fsys = LocalFS(tmp_path)
        source = _DictContentSource({"d1": b"aaa", "d2": b"bbb"})
        prepare_steps(plan, fsys, "obj1", source)

        # Swap inventory (same step count, different content digest)
        plan.new_inv = inv2

        with pytest.raises(ValueError, match="mismatch"):
            prepare_steps(plan, fsys, "obj1", source)

    def test_resume_full_execution_idempotent(self, tmp_path: Path):
        """Resuming a fully completed plan is a no-op."""
        new_inv = _build_v1_inventory()
        plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)
        fsys = LocalFS(tmp_path)
        source = _DictContentSource({"aabbcc": b"hello"})

        prepare_steps(plan, fsys, "obj1", source)
        execute_plan(plan)

        # Serialize/deserialize fully completed plan
        data = serialize(plan)
        restored = deserialize(data)
        prepare_steps(restored, fsys, "obj1", source)

        # All steps already completed
        assert all(s.completed for s in restored.steps)

        # Execute again — should be a no-op
        execute_plan(restored)
        assert (tmp_path / "obj1" / "inventory.json").read_bytes() == new_inv.raw_bytes


class TestExhaustiveFaultTolerance:
    """ocfl-go style: fail at every step index, verify revert + clean state."""

    def test_fail_at_each_step_new_object(self, tmp_path: Path):
        """For each step index: fail → revert → verify obj dir removed."""
        files = {"a.txt": "d1", "b.txt": "d2"}
        new_inv = _build_v1_inventory(files=files)
        source_files = {"d1": b"aaa", "d2": b"bbb"}

        # First, count total steps
        plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)
        fsys = LocalFS(tmp_path / "count")
        source = _DictContentSource(source_files)
        prepare_steps(plan, fsys, "obj1", source)
        total_steps = len(plan.steps)

        for fail_idx in range(total_steps):
            obj_dir = tmp_path / f"fail_{fail_idx}"
            fsys = LocalFS(obj_dir)
            plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)
            source = _DictContentSource(source_files)
            prepare_steps(plan, fsys, "obj1", source)

            # Replace run callable at fail_idx
            plan.steps[fail_idx].run = _failing_step

            with pytest.raises(RuntimeError, match="disk full"):
                execute_plan(plan)

            # Revert completed steps
            revert_plan(plan)

            # New object: entire obj dir should be gone
            assert not (obj_dir / "obj1").exists(), (
                f"obj dir remains after fail at step {fail_idx} "
                f"({plan.steps[fail_idx].name})"
            )

    def test_fail_at_each_step_existing_object(self, tmp_path: Path):
        """Fail at each step during v2 update → revert → v1 intact."""
        # Create v1 first
        old_inv = _build_v1_inventory(files={"a.txt": "d1"})
        plan1 = UpdatePlan(steps=[], new_inv=old_inv, old_inv=None)
        fsys = LocalFS(tmp_path)
        source1 = _DictContentSource({"d1": b"aaa"})
        prepare_steps(plan1, fsys, "obj1", source1)
        execute_plan(plan1)

        # Save v1 state for comparison
        v1_inv_bytes = (tmp_path / "obj1" / "inventory.json").read_bytes()

        # Build v2 plan to count steps
        new_inv = _build_v2_inventory(old_inv, new_files={"a.txt": "d1", "b.txt": "d2"})
        plan_count = UpdatePlan(steps=[], new_inv=new_inv, old_inv=old_inv)
        source_count = _DictContentSource({"d2": b"bbb"})
        prepare_steps(plan_count, fsys, "obj1", source_count)
        total_steps = len(plan_count.steps)

        for fail_idx in range(total_steps):
            plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=old_inv)
            source = _DictContentSource({"d2": b"bbb"})
            prepare_steps(plan, fsys, "obj1", source)

            plan.steps[fail_idx].run = _failing_step

            with pytest.raises(RuntimeError, match="disk full"):
                execute_plan(plan)

            revert_plan(plan)

            # v1 must be intact
            assert (tmp_path / "obj1" / "v1" / "content" / "a.txt").is_file(), (
                f"v1 content missing after fail at step {fail_idx}"
            )
            # v2 dir should not exist
            assert not (tmp_path / "obj1" / "v2").exists(), (
                f"v2 dir remains after fail at step {fail_idx}"
            )
            # Root inventory restored to v1
            assert (
                tmp_path / "obj1" / "inventory.json"
            ).read_bytes() == v1_inv_bytes, (
                f"root inventory not restored after fail at step {fail_idx}"
            )

    def test_revert_and_redo_after_fail_at_each_step(self, tmp_path: Path):
        """Fail at step N → revert → redo from scratch → valid object."""
        files = {"a.txt": "d1", "b.txt": "d2"}
        new_inv = _build_v1_inventory(files=files)
        source_files = {"d1": b"aaa", "d2": b"bbb"}

        # Count steps
        plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)
        fsys = LocalFS(tmp_path / "count")
        source = _DictContentSource(source_files)
        prepare_steps(plan, fsys, "obj1", source)
        total_steps = len(plan.steps)

        for fail_idx in range(total_steps):
            obj_dir = tmp_path / f"resume_{fail_idx}"
            fsys = LocalFS(obj_dir)
            plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)
            source = _DictContentSource(source_files)
            prepare_steps(plan, fsys, "obj1", source)

            # Fail at this step
            plan.steps[fail_idx].run = _failing_step

            with pytest.raises(RuntimeError, match="disk full"):
                execute_plan(plan)

            # Verify serialization round-trips mid-failure state
            data = serialize(plan)
            deserialize(data)  # validates no crash on partial state

            # Revert the failed attempt
            revert_plan(plan)

            # Resume from scratch with fresh plan
            fresh_plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)
            source2 = _DictContentSource(source_files)
            prepare_steps(fresh_plan, fsys, "obj1", source2)
            execute_plan(fresh_plan)

            # Object must be valid
            assert (
                obj_dir / "obj1" / "inventory.json"
            ).read_bytes() == new_inv.raw_bytes, (
                f"inventory mismatch after resume from fail at step {fail_idx}"
            )
            assert (
                obj_dir / "obj1" / "v1" / "content" / "a.txt"
            ).read_bytes() == b"aaa"
            assert (
                obj_dir / "obj1" / "v1" / "content" / "b.txt"
            ).read_bytes() == b"bbb"

            # Clean up for next iteration
            shutil.rmtree(obj_dir / "obj1")

    def test_keyboard_interrupt_at_each_step(self, tmp_path: Path):
        """KeyboardInterrupt at each step → revert → clean state."""
        new_inv = _build_v1_inventory()
        source_files = {"aabbcc": b"hello"}

        plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)
        fsys = LocalFS(tmp_path / "count")
        source = _DictContentSource(source_files)
        prepare_steps(plan, fsys, "obj1", source)
        total_steps = len(plan.steps)

        def _interrupt() -> int:
            raise KeyboardInterrupt

        for fail_idx in range(total_steps):
            obj_dir = tmp_path / f"intr_{fail_idx}"
            fsys = LocalFS(obj_dir)
            plan = UpdatePlan(steps=[], new_inv=new_inv, old_inv=None)
            source = _DictContentSource(source_files)
            prepare_steps(plan, fsys, "obj1", source)

            plan.steps[fail_idx].run = _interrupt

            with pytest.raises(KeyboardInterrupt):
                execute_plan(plan)

            # Steps after the interrupted one should not have executed
            for step in plan.steps[fail_idx + 1 :]:
                assert not step.completed, (
                    f"step {step.name!r} ran after interrupt at step {fail_idx}"
                )

            revert_plan(plan)

            assert not (obj_dir / "obj1").exists(), (
                f"obj dir remains after KeyboardInterrupt at step {fail_idx}"
            )


class TestPlanWireVersion:
    def test_roundtrip_includes_version(self):
        import json

        from ocfl_lib.update_plan import deserialize, serialize

        plan = _make_plan()
        data = serialize(plan)
        decoded = json.loads(data)
        assert decoded.get("version") == 1
        deserialize(data)  # roundtrip ok

    def test_bumped_version_raises(self):
        import json

        import pytest

        from ocfl_lib.update_plan import deserialize, serialize

        plan = _make_plan()
        data = serialize(plan)
        decoded = json.loads(data)
        decoded["version"] = 999
        with pytest.raises(ValueError, match="wire version"):
            deserialize(json.dumps(decoded).encode())


def _make_plan():
    """Build a minimal plan for serialization tests."""
    from ocfl_lib.digest import DigestAlgorithm
    from ocfl_lib.inventory import StoredInventory
    from ocfl_lib.inventory_builder import InventoryBuilder
    from ocfl_lib.update_plan import UpdatePlan

    inv = (
        InventoryBuilder()
        .id("test")
        .add_version({"abc": ["f.txt"]}, created="2024-01-01T00:00:00Z")
        .build()
    )
    stored = StoredInventory.from_inventory(inv, DigestAlgorithm.SHA512)
    return UpdatePlan(steps=[], new_inv=stored, old_inv=None)
