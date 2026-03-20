"""Tests for OCFL Object read operations."""

from typing import TYPE_CHECKING

import pytest

from ocfl_lib.fs.local import LocalFS
from ocfl_lib.object import Object
from ocfl_lib.spec import Spec, VNum

if TYPE_CHECKING:
    from pathlib import Path


class TestObjectReadExisting:
    """7.1: Object class — read existing objects from fixtures."""

    def test_exists_true(self, good_objects_1_1: Path):
        obj_path = good_objects_1_1 / "minimal_one_version_one_file"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        assert obj.exists

    def test_exists_false(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        obj = Object(fsys, "")
        assert not obj.exists

    def test_id(self, good_objects_1_1: Path):
        obj_path = good_objects_1_1 / "minimal_one_version_one_file"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        assert obj.id == "ark:123/abc"

    def test_spec_1_1(self, good_objects_1_1: Path):
        obj_path = good_objects_1_1 / "minimal_one_version_one_file"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        assert obj.spec == Spec.V1_1

    def test_spec_1_0(self, good_objects_1_0: Path):
        obj_path = good_objects_1_0 / "minimal_one_version_one_file"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        assert obj.spec == Spec.V1_0

    def test_head_single_version(self, good_objects_1_1: Path):
        obj_path = good_objects_1_1 / "minimal_one_version_one_file"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        assert obj.head == VNum(1)

    def test_head_multi_version(self, good_objects_1_1: Path):
        obj_path = good_objects_1_1 / "spec-ex-full"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        assert obj.head == VNum(3)

    def test_inventory_returns_stored_inventory(self, good_objects_1_1: Path):
        obj_path = good_objects_1_1 / "minimal_one_version_one_file"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        stored = obj.inventory
        assert stored.inventory.id == "ark:123/abc"
        assert stored.digest  # non-empty digest

    def test_inventory_cached(self, good_objects_1_1: Path):
        obj_path = good_objects_1_1 / "minimal_one_version_one_file"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        inv1 = obj.inventory
        inv2 = obj.inventory
        assert inv1 is inv2

    def test_versions_single(self, good_objects_1_1: Path):
        obj_path = good_objects_1_1 / "minimal_one_version_one_file"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        assert obj.versions() == [VNum(1)]

    def test_versions_multiple(self, good_objects_1_1: Path):
        obj_path = good_objects_1_1 / "spec-ex-full"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        assert obj.versions() == [VNum(1), VNum(2), VNum(3)]

    def test_version_inventory_head(self, good_objects_1_1: Path):
        """Head version inventory should exist and match root."""
        obj_path = good_objects_1_1 / "minimal_one_version_one_file"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        vinv = obj.version_inventory(VNum(1))
        assert vinv is not None
        assert vinv.inventory.id == obj.id

    def test_version_inventory_missing(self, good_objects_1_1: Path):
        """Version inventory for non-existent version returns None."""
        obj_path = good_objects_1_1 / "minimal_one_version_one_file"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        assert obj.version_inventory(VNum(99)) is None

    def test_sidecar_validated_on_load(self, tmp_path: Path):
        """Sidecar mismatch raises on inventory load."""
        # Create a minimal object with a bad sidecar
        obj_dir = tmp_path / "obj"
        obj_dir.mkdir()
        (obj_dir / "0=ocfl_object_1.1").write_text("ocfl_object_1.1\n")
        inv = (
            '{"id":"test:1","type":"https://ocfl.io/1.1/spec/#inventory",'
            '"digestAlgorithm":"sha512","head":"v1",'
            '"manifest":{},"versions":{"v1":{"created":"2024-01-01T00:00:00Z","state":{}}}}'
        )
        (obj_dir / "inventory.json").write_text(inv)
        (obj_dir / "inventory.json.sha512").write_text("bad_digest inventory.json\n")
        fsys = LocalFS(obj_dir)
        obj = Object(fsys, "")
        with pytest.raises(ValueError, match="sidecar"):
            _ = obj.inventory

    @pytest.mark.parametrize(
        "fixture_name",
        [
            "minimal_one_version_one_file",
            "minimal_no_content",
            "spec-ex-full",
            "updates_three_versions_one_file",
            "updates_all_actions",
        ],
    )
    def test_good_objects_1_1_readable(self, good_objects_1_1: Path, fixture_name: str):
        obj_path = good_objects_1_1 / fixture_name
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        assert obj.exists
        assert obj.id
        assert obj.head.number >= 1
        assert len(obj.versions()) == obj.head.number
