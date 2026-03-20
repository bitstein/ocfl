"""Tests for update_plan module — data structures and serialization."""

from ocfl_lib.digest import DigestAlgorithm
from ocfl_lib.inventory import Inventory, StoredInventory, VersionInfo
from ocfl_lib.update_plan import (
    ContentSource,
    PlanStep,
    UpdatePlan,
    deserialize,
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
