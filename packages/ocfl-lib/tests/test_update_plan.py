"""Tests for update_plan module."""

from pathlib import Path
from typing import TYPE_CHECKING

from ocfl_lib.digest import DigestAlgorithm
from ocfl_lib.fs.local import LocalFS
from ocfl_lib.inventory import Inventory, StoredInventory, VersionInfo

if TYPE_CHECKING:
    from ocfl_lib.fs.protocols import FS
from ocfl_lib.inventory_builder import InventoryBuilder
from ocfl_lib.update_plan import (
    ContentSource,
    PlanStep,
    UpdatePlan,
    deserialize,
    prepare_steps,
    serialize,
)


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


class TestContentSourceProtocol:
    def test_protocol_checkable(self):
        """ContentSource is a runtime-checkable protocol."""

        class MySource:
            def get_content(self, digest: str) -> tuple:
                return (None, "")

        assert isinstance(MySource(), ContentSource)

    def test_non_conforming(self):
        class NotASource:
            pass

        assert not isinstance(NotASource(), ContentSource)


class _DictContentSource:
    """ContentSource backed by a dict of digest → bytes."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = files

    def get_content(self, digest: str) -> tuple[FS, str]:
        """Return an FS and path that will yield the content bytes."""
        # Use a simple in-memory FS-like wrapper via a temp approach
        # For testing, we store to a temp dir on first access
        if not hasattr(self, "_fs"):
            import tempfile

            self._tmpdir = Path(tempfile.mkdtemp())
            self._fs = LocalFS(self._tmpdir)
            for d, data in self._files.items():
                fpath = self._tmpdir / d
                fpath.write_bytes(data)
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
