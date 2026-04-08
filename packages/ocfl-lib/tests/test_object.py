"""Tests for OCFL Object read operations."""

from typing import TYPE_CHECKING

import pytest

from ocfl_lib.errors import InventorySidecarMismatch, VersionNotFound
from ocfl_lib.fs.local import LocalFS
from ocfl_lib.inventory import VersionInfo
from ocfl_lib.object import DiffEntry, Object
from ocfl_lib.spec import Spec, VNum

if TYPE_CHECKING:
    from pathlib import Path


class TestObjectReadExisting:
    """Object class — read existing objects from fixtures."""

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
        with pytest.raises(InventorySidecarMismatch, match="sidecar"):
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


class TestVersionContentAccess:
    """Version content access."""

    def test_version_state(self, good_objects_1_1: Path):
        obj_path = good_objects_1_1 / "minimal_one_version_one_file"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        state = obj.version_state(VNum(1))
        # state is digest → [logical_paths]
        assert len(state) == 1
        paths = next(iter(state.values()))
        assert paths == ["a_file.txt"]

    def test_version_state_multi_version(self, good_objects_1_1: Path):
        obj_path = good_objects_1_1 / "spec-ex-full"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        # v1 has 3 files: foo/bar.xml, empty.txt, image.tiff
        state_v1 = obj.version_state(VNum(1))
        v1_paths = sorted(p for paths in state_v1.values() for p in paths)
        assert v1_paths == ["empty.txt", "foo/bar.xml", "image.tiff"]
        # v2: foo/bar.xml modified, image.tiff removed, empty2.txt added
        state_v2 = obj.version_state(VNum(2))
        v2_paths = sorted(p for paths in state_v2.values() for p in paths)
        assert v2_paths == ["empty.txt", "empty2.txt", "foo/bar.xml"]

    def test_version_info(self, good_objects_1_1: Path):
        obj_path = good_objects_1_1 / "minimal_one_version_one_file"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        info = obj.version_info(VNum(1))
        assert isinstance(info, VersionInfo)
        assert info.created == "2019-01-01T02:03:04Z"
        assert info.message == "An version with one file"
        assert info.user is not None
        assert info.user.name == "A Person"

    def test_version_info_invalid_version(self, good_objects_1_1: Path):
        obj_path = good_objects_1_1 / "minimal_one_version_one_file"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        with pytest.raises(VersionNotFound):
            obj.version_info(VNum(99))

    def test_content_path(self, good_objects_1_1: Path):
        obj_path = good_objects_1_1 / "minimal_one_version_one_file"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        # Get the digest from state
        state = obj.version_state(VNum(1))
        digest = next(iter(state.keys()))
        cp = obj.content_path(digest)
        assert cp == "v1/content/a_file.txt"

    def test_content_path_missing(self, good_objects_1_1: Path):
        obj_path = good_objects_1_1 / "minimal_one_version_one_file"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        assert obj.content_path("nonexistent_digest") is None

    def test_open_logical_path(self, good_objects_1_1: Path):
        obj_path = good_objects_1_1 / "minimal_one_version_one_file"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        with obj.open_logical_path(VNum(1), "a_file.txt") as f:
            content = f.read()
        assert content == b"Hello! I am a file.\n"

    def test_open_logical_path_multi_version(self, good_objects_1_1: Path):
        """Read same logical path from different versions gets different content."""
        obj_path = good_objects_1_1 / "updates_three_versions_one_file"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        with obj.open_logical_path(VNum(1), "a_file.txt") as f:
            v1_content = f.read()
        with obj.open_logical_path(VNum(2), "a_file.txt") as f:
            v2_content = f.read()
        assert v1_content != v2_content

    def test_open_logical_path_missing_key(self, good_objects_1_1: Path):
        obj_path = good_objects_1_1 / "minimal_one_version_one_file"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        with pytest.raises(KeyError):
            obj.open_logical_path(VNum(1), "nonexistent.txt")

    def test_open_logical_path_custom_content_dir(self, good_objects_1_1: Path):
        """Object with custom contentDirectory should still resolve paths."""
        obj_path = good_objects_1_1 / "minimal_content_dir_called_stuff"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        # This fixture uses contentDirectory="stuff"
        state = obj.version_state(VNum(1))
        logical_path = next(iter(state.values()))[0]
        with obj.open_logical_path(VNum(1), logical_path) as f:
            assert f.read()  # non-empty content

    def test_open_logical_path_cross_version_dedup(self, good_objects_1_1: Path):
        """v3 reuses image.tiff from v1 content — manifest resolves across versions."""
        obj_path = good_objects_1_1 / "spec-ex-full"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        # v3 state has image.tiff, but content lives in v1/content/
        with obj.open_logical_path(VNum(3), "image.tiff") as f:
            data = f.read()
        assert len(data) > 0


class TestObjectDiff:
    """Object diff between versions."""

    def test_diff_no_changes(self, good_objects_1_1: Path):
        """Diffing a version against itself yields no entries."""
        obj_path = good_objects_1_1 / "minimal_one_version_one_file"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        diff = obj.diff_versions(VNum(1), VNum(1))
        assert diff == []

    def test_diff_modified_file(self, good_objects_1_1: Path):
        """v1→v2: a_file.txt modified (different digest, same path)."""
        obj_path = good_objects_1_1 / "updates_three_versions_one_file"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        diff = obj.diff_versions(VNum(1), VNum(2))
        assert len(diff) == 1
        entry = diff[0]
        assert entry.path == "a_file.txt"
        assert entry.action == "modified"
        assert entry.digest_before is not None
        assert entry.digest_after is not None
        assert entry.digest_before != entry.digest_after

    def test_diff_spec_ex_full_v1_v2(self, good_objects_1_1: Path):
        """v1→v2: bar.xml modified, image.tiff deleted, empty2.txt added."""
        obj_path = good_objects_1_1 / "spec-ex-full"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        diff = obj.diff_versions(VNum(1), VNum(2))
        by_path = {e.path: e for e in diff}

        assert by_path["foo/bar.xml"].action == "modified"
        assert by_path["image.tiff"].action == "deleted"
        assert by_path["empty2.txt"].action == "added"

    def test_diff_spec_ex_full_v2_v3(self, good_objects_1_1: Path):
        """spec-ex-full v2→v3: image.tiff reinstated, empty.txt deleted."""
        obj_path = good_objects_1_1 / "spec-ex-full"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        diff = obj.diff_versions(VNum(2), VNum(3))
        by_path = {e.path: e for e in diff}

        assert by_path["image.tiff"].action == "added"
        assert by_path["empty.txt"].action == "deleted"

    def test_diff_rename_detection(self, good_objects_1_1: Path):
        """v1→v2: poe.txt renamed to poe-nevermore.txt (same digest)."""
        obj_path = good_objects_1_1 / "updates_all_actions"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        diff = obj.diff_versions(VNum(1), VNum(2))
        renames = [e for e in diff if e.action == "renamed"]
        assert len(renames) == 1
        rename = renames[0]
        assert rename.path == "my_content/poe-nevermore.txt"
        assert rename.renamed_from == "my_content/poe.txt"

    def test_diff_entry_fields(self, good_objects_1_1: Path):
        """DiffEntry has expected fields for an added file."""
        obj_path = good_objects_1_1 / "spec-ex-full"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        diff = obj.diff_versions(VNum(1), VNum(2))
        added = [e for e in diff if e.action == "added"]
        assert len(added) == 1
        entry = added[0]
        assert isinstance(entry, DiffEntry)
        assert entry.digest_before is None
        assert entry.digest_after is not None
        assert entry.renamed_from is None

    def test_diff_deleted_entry_fields(self, good_objects_1_1: Path):
        """DiffEntry for deleted file has digest_before but no digest_after."""
        obj_path = good_objects_1_1 / "spec-ex-full"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        diff = obj.diff_versions(VNum(1), VNum(2))
        deleted = [e for e in diff if e.action == "deleted"]
        assert len(deleted) == 1
        entry = deleted[0]
        assert entry.digest_before is not None
        assert entry.digest_after is None
        assert entry.renamed_from is None

    def test_diff_updates_all_actions_v2_v3(self, good_objects_1_1: Path):
        """v2→v3: a_second_copy removed, poe-nevermore modified."""
        obj_path = good_objects_1_1 / "updates_all_actions"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        diff = obj.diff_versions(VNum(2), VNum(3))
        by_path = {e.path: e for e in diff}

        assert by_path["my_content/a_second_copy_of_dracula.txt"].action == "deleted"
        assert by_path["my_content/poe-nevermore.txt"].action == "modified"
