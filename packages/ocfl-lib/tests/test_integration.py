"""Full round-trip integration tests.

Create storage root → commit object → add version → validate → read back.
Tests every layout type, fixity algorithms, and on-disk structure.
"""

import json
from typing import TYPE_CHECKING

import pytest

from ocfl_lib.digest import DigestAlgorithm
from ocfl_lib.fs.local import LocalFS
from ocfl_lib.inventory import UserInfo
from ocfl_lib.layout import (
    FlatDirectLayout,
    FlatOmitPrefixLayout,
    HashAndIdNTupleLayout,
    HashedNTupleLayout,
    NTupleOmitPrefixLayout,
)
from ocfl_lib.root import StorageRoot
from ocfl_lib.spec import Spec, VNum
from ocfl_lib.stage import stage_dir
from ocfl_lib.validation.object import validate_object
from ocfl_lib.validation.result import ValidationResult
from ocfl_lib.validation.root import validate_root

if TYPE_CHECKING:
    from pathlib import Path

    from ocfl_lib.layout.types import Layout


def _write_files(base: Path, files: dict[str, str]) -> None:
    """Create files under base directory."""
    for name, content in files.items():
        p = base / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


# Object ID templates per layout (base used for single, suffixed for multi)
_LAYOUT_ID_BASE = {
    "flat-direct": "obj",
    "hashed-n-tuple": "http://example.org/obj",
    "hash-and-id-n-tuple": "http://example.org/obj",
    "flat-omit-prefix": "ns:obj",
    "n-tuple-omit-prefix": "ns:obj",
}


def _make_layout(name: str) -> Layout:
    """Instantiate a layout by short name."""
    match name:
        case "flat-direct":
            return FlatDirectLayout()
        case "hashed-n-tuple":
            return HashedNTupleLayout()
        case "hash-and-id-n-tuple":
            return HashAndIdNTupleLayout()
        case "flat-omit-prefix":
            return FlatOmitPrefixLayout(delimiter=":")
        case "n-tuple-omit-prefix":
            return NTupleOmitPrefixLayout(delimiter=":")
        case _:
            raise ValueError(f"unknown layout: {name}")


ALL_LAYOUTS = [
    "flat-direct",
    "hashed-n-tuple",
    "hash-and-id-n-tuple",
    "flat-omit-prefix",
    "n-tuple-omit-prefix",
]


class TestRoundTripAllLayouts:
    """Full round-trip with every layout type."""

    @pytest.mark.parametrize("layout_name", ALL_LAYOUTS)
    def test_create_commit_validate_read(self, tmp_path: Path, layout_name: str):
        """Init root → commit v1 → commit v2 → validate root → read back."""
        layout = _make_layout(layout_name)
        obj_id = _LAYOUT_ID_BASE[layout_name]

        root_dir = tmp_path / "root"
        root_dir.mkdir()
        fsys = LocalFS(root_dir)
        root = StorageRoot.init(fsys, fsys, layout=layout)

        # v1: two files (source outside root)
        src1 = tmp_path / "src1"
        src1.mkdir()
        _write_files(src1, {"hello.txt": "hello", "dir/nested.txt": "nested"})
        stage1 = stage_dir(LocalFS(tmp_path), "src1", DigestAlgorithm.SHA512)
        obj = root.commit(
            fsys,
            obj_id,
            stage1,
            message="first version",
            user=UserInfo(name="Alice", address="mailto:alice@example.org"),
            created="2024-01-01T00:00:00Z",
        )

        assert obj.id == obj_id
        assert obj.head == VNum(1)

        # v2: modify one, add one, remove one
        src2 = tmp_path / "src2"
        src2.mkdir()
        _write_files(src2, {"hello.txt": "hello modified", "new.txt": "brand new"})
        stage2 = stage_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)
        obj = root.commit(
            fsys,
            obj_id,
            stage2,
            message="second version",
            user=UserInfo(name="Bob"),
            created="2024-06-01T00:00:00Z",
        )

        assert obj.head == VNum(2)

        # Validate entire root
        result = ValidationResult()
        summary = validate_root(fsys, "", result)
        assert result.is_valid, f"errors: {[e.message for e in result.errors]}"
        assert summary.num_objects == 1
        assert summary.valid_objects == 1

        # Read back v1 content
        with obj.open_logical_path(VNum(1), "hello.txt") as f:
            assert f.read() == b"hello"
        with obj.open_logical_path(VNum(1), "dir/nested.txt") as f:
            assert f.read() == b"nested"

        # Read back v2 content
        with obj.open_logical_path(VNum(2), "hello.txt") as f:
            assert f.read() == b"hello modified"
        with obj.open_logical_path(VNum(2), "new.txt") as f:
            assert f.read() == b"brand new"

        # v2 should not have dir/nested.txt
        with pytest.raises(KeyError, match=r"dir/nested\.txt"):
            obj.open_logical_path(VNum(2), "dir/nested.txt")

        # Version metadata preserved
        v1_info = obj.version_info(VNum(1))
        assert v1_info.message == "first version"
        assert v1_info.user is not None
        assert v1_info.user.name == "Alice"
        assert v1_info.created == "2024-01-01T00:00:00Z"

        v2_info = obj.version_info(VNum(2))
        assert v2_info.message == "second version"
        assert v2_info.user is not None
        assert v2_info.user.name == "Bob"

    @pytest.mark.parametrize("layout_name", ALL_LAYOUTS)
    def test_multiple_objects_per_root(self, tmp_path: Path, layout_name: str):
        """Multiple objects in one root, all discoverable via objects()."""
        layout = _make_layout(layout_name)
        root_dir = tmp_path / "root"
        root_dir.mkdir()
        fsys = LocalFS(root_dir)
        root = StorageRoot.init(fsys, fsys, layout=layout)

        base = _LAYOUT_ID_BASE[layout_name]
        ids = [f"{base}-{i}" for i in range(3)]

        for i, oid in enumerate(ids):
            src = tmp_path / f"src-{i}"
            src.mkdir()
            _write_files(src, {"data.txt": f"content-{i}"})
            stage = stage_dir(LocalFS(tmp_path), f"src-{i}", DigestAlgorithm.SHA512)
            root.commit(fsys, oid, stage, created="2024-01-01T00:00:00Z")

        # All objects discoverable
        found_ids = sorted(o.id for o in root.objects())
        assert found_ids == sorted(ids)

        # Root validates
        result = ValidationResult()
        summary = validate_root(fsys, "", result)
        assert result.is_valid
        assert summary.num_objects == 3
        assert summary.valid_objects == 3


class TestFixityRoundTrip:
    """Round-trip with fixity algorithms."""

    def test_fixity_single_algorithm(self, tmp_path: Path):
        """Commit with SHA256 fixity, validate, read back fixity block."""
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        src = tmp_path / "src"
        src.mkdir()
        _write_files(src, {"a.txt": "aaa", "b.txt": "bbb"})
        stage = stage_dir(
            LocalFS(tmp_path),
            "src",
            DigestAlgorithm.SHA512,
            fixity_algs=[DigestAlgorithm.SHA256],
        )

        obj = root.commit(fsys, "fix-obj", stage, created="2024-01-01T00:00:00Z")

        inv = obj.inventory.inventory
        assert inv.fixity is not None
        assert "sha256" in inv.fixity
        # Each content file should have a sha256 fixity entry
        sha256_paths = sorted(p for ps in inv.fixity["sha256"].values() for p in ps)
        manifest_paths = sorted(p for ps in inv.manifest.values() for p in ps)
        assert sha256_paths == manifest_paths

        # Validates with fixity
        result = ValidationResult()
        validate_object(fsys, "fix-obj", result)
        assert result.is_valid

    def test_fixity_multiple_algorithms(self, tmp_path: Path):
        """Commit with multiple fixity algorithms."""
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        src = tmp_path / "src"
        src.mkdir()
        _write_files(src, {"file.txt": "content"})
        stage = stage_dir(
            LocalFS(tmp_path),
            "src",
            DigestAlgorithm.SHA512,
            fixity_algs=[DigestAlgorithm.SHA256, DigestAlgorithm.MD5],
        )

        obj = root.commit(fsys, "multi-fix", stage, created="2024-01-01T00:00:00Z")

        inv = obj.inventory.inventory
        assert inv.fixity is not None
        assert "sha256" in inv.fixity
        assert "md5" in inv.fixity

        result = ValidationResult()
        validate_object(fsys, "multi-fix", result)
        assert result.is_valid

    def test_fixity_preserved_across_versions(self, tmp_path: Path):
        """Fixity from v1 preserved in v2 inventory for shared content."""
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        # v1 with fixity
        src1 = tmp_path / "src1"
        src1.mkdir()
        _write_files(src1, {"keep.txt": "stable", "change.txt": "old"})
        stage1 = stage_dir(
            LocalFS(tmp_path),
            "src1",
            DigestAlgorithm.SHA512,
            fixity_algs=[DigestAlgorithm.SHA256],
        )
        root.commit(fsys, "fix-v2", stage1, created="2024-01-01T00:00:00Z")

        # v2 with fixity — keep.txt unchanged
        src2 = tmp_path / "src2"
        src2.mkdir()
        _write_files(src2, {"keep.txt": "stable", "change.txt": "new"})
        stage2 = stage_dir(
            LocalFS(tmp_path),
            "src2",
            DigestAlgorithm.SHA512,
            fixity_algs=[DigestAlgorithm.SHA256],
        )
        obj = root.commit(fsys, "fix-v2", stage2, created="2024-06-01T00:00:00Z")

        inv = obj.inventory.inventory
        assert inv.fixity is not None
        assert "sha256" in inv.fixity
        # All manifest content paths should have fixity
        all_manifest_paths = sorted(p for ps in inv.manifest.values() for p in ps)
        all_fixity_paths = sorted(p for ps in inv.fixity["sha256"].values() for p in ps)
        assert all_fixity_paths == all_manifest_paths

        result = ValidationResult()
        validate_object(fsys, "fix-v2", result)
        assert result.is_valid


class TestOnDiskStructure:
    """Verify on-disk structure matches OCFL spec exactly."""

    def test_object_structure_v1(self, tmp_path: Path):
        """Single-version object has correct on-disk layout."""
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        src = tmp_path / "src"
        src.mkdir()
        _write_files(src, {"file.txt": "hello"})
        stage = stage_dir(LocalFS(tmp_path), "src", DigestAlgorithm.SHA512)
        root.commit(fsys, "struct-obj", stage, created="2024-01-01T00:00:00Z")

        obj_dir = tmp_path / "struct-obj"
        assert obj_dir.is_dir()

        # NAMASTE declaration
        assert (obj_dir / "0=ocfl_object_1.1").exists()
        assert (obj_dir / "0=ocfl_object_1.1").read_text() == "ocfl_object_1.1\n"

        # Root inventory + sidecar
        assert (obj_dir / "inventory.json").exists()
        assert (obj_dir / "inventory.json.sha512").exists()

        # Version directory
        v1_dir = obj_dir / "v1"
        assert v1_dir.is_dir()

        # Version inventory + sidecar
        assert (v1_dir / "inventory.json").exists()
        assert (v1_dir / "inventory.json.sha512").exists()

        # Content directory
        content_dir = v1_dir / "content"
        assert content_dir.is_dir()
        assert (content_dir / "file.txt").exists()
        assert (content_dir / "file.txt").read_text() == "hello"

        # Root and version inventories are identical
        assert (obj_dir / "inventory.json").read_bytes() == (
            v1_dir / "inventory.json"
        ).read_bytes()

        # Sidecar format: "{digest} inventory.json\n"
        sidecar = (obj_dir / "inventory.json.sha512").read_text()
        parts = sidecar.strip().split()
        assert len(parts) == 2
        assert parts[1] == "inventory.json"
        assert len(parts[0]) == 128  # sha512 hex length

    def test_object_structure_v2_forward_delta(self, tmp_path: Path):
        """Two-version object: v2 content dir only has new/changed files."""
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        src1 = tmp_path / "src1"
        src1.mkdir()
        _write_files(src1, {"keep.txt": "stable", "change.txt": "old"})
        stage1 = stage_dir(LocalFS(tmp_path), "src1", DigestAlgorithm.SHA512)
        root.commit(fsys, "delta-obj", stage1, created="2024-01-01T00:00:00Z")

        src2 = tmp_path / "src2"
        src2.mkdir()
        _write_files(src2, {"keep.txt": "stable", "change.txt": "new"})
        stage2 = stage_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)
        root.commit(fsys, "delta-obj", stage2, created="2024-06-01T00:00:00Z")

        obj_dir = tmp_path / "delta-obj"

        # v1 has both files
        v1_content = obj_dir / "v1" / "content"
        assert (v1_content / "keep.txt").exists()
        assert (v1_content / "change.txt").exists()

        # v2 content only has changed file (keep.txt deduplicated)
        v2_content = obj_dir / "v2" / "content"
        assert v2_content.is_dir()
        v2_files = sorted(p.name for p in v2_content.rglob("*") if p.is_file())
        assert v2_files == ["change.txt"]

        # Both versions have inventory copies
        assert (obj_dir / "v2" / "inventory.json").exists()

        # Root inventory is latest
        root_inv = json.loads((obj_dir / "inventory.json").read_bytes())
        assert root_inv["head"] == "v2"

    def test_root_structure(self, tmp_path: Path):
        """Storage root on-disk structure."""
        layout = HashedNTupleLayout()
        fsys = LocalFS(tmp_path)
        StorageRoot.init(fsys, fsys, layout=layout, layout_description="test layout")

        # NAMASTE
        assert (tmp_path / "0=ocfl_1.1").exists()

        # ocfl_layout.json
        layout_data = json.loads((tmp_path / "ocfl_layout.json").read_bytes())
        assert layout_data["extension"] == "0004-hashed-n-tuple-storage-layout"
        assert layout_data["description"] == "test layout"

        # Extension config
        ext_config_path = (
            tmp_path
            / "extensions"
            / "0004-hashed-n-tuple-storage-layout"
            / "config.json"
        )
        assert ext_config_path.exists()
        ext_config = json.loads(ext_config_path.read_bytes())
        assert "digestAlgorithm" in ext_config
        assert "tupleSize" in ext_config
        assert "numberOfTuples" in ext_config

    def test_inventory_json_structure(self, tmp_path: Path):
        """Inventory JSON has correct OCFL fields."""
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        src = tmp_path / "src"
        src.mkdir()
        _write_files(src, {"file.txt": "content"})
        stage = stage_dir(LocalFS(tmp_path), "src", DigestAlgorithm.SHA512)
        root.commit(
            fsys,
            "inv-test",
            stage,
            message="test",
            user=UserInfo(name="Tester", address="mailto:test@example.org"),
            created="2024-01-01T00:00:00Z",
        )

        inv = json.loads((tmp_path / "inv-test" / "inventory.json").read_bytes())

        # Required fields
        assert inv["id"] == "inv-test"
        assert inv["type"] == "https://ocfl.io/1.1/spec/#inventory"
        assert inv["digestAlgorithm"] == "sha512"
        assert inv["head"] == "v1"
        assert isinstance(inv["manifest"], dict)
        assert isinstance(inv["versions"], dict)

        # camelCase field names (not snake_case)
        assert "digest_algorithm" not in inv
        assert "content_directory" not in inv

        # Version block structure
        v1 = inv["versions"]["v1"]
        assert v1["created"] == "2024-01-01T00:00:00Z"
        assert v1["message"] == "test"
        assert v1["user"]["name"] == "Tester"
        assert v1["user"]["address"] == "mailto:test@example.org"
        assert isinstance(v1["state"], dict)


class TestSpecVersions:
    """Round-trip with both OCFL spec versions."""

    @pytest.mark.parametrize("spec", [Spec.V1_0, Spec.V1_1])
    def test_spec_round_trip(self, tmp_path: Path, spec: Spec):
        """Create and validate with each spec version."""
        root_dir = tmp_path / "root"
        root_dir.mkdir()
        fsys = LocalFS(root_dir)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout(), spec=spec)
        assert root.spec == spec

        src = tmp_path / "src"
        src.mkdir()
        _write_files(src, {"file.txt": "content"})
        stage = stage_dir(LocalFS(tmp_path), "src", DigestAlgorithm.SHA512)
        obj = root.commit(fsys, "spec-obj", stage, created="2024-01-01T00:00:00Z")

        assert obj.spec == spec

        result = ValidationResult()
        validate_root(fsys, "", result)
        assert result.is_valid

        # NAMASTE matches spec
        expected_namaste = f"0=ocfl_{spec.value}"
        assert (root_dir / expected_namaste).exists()

    def test_sha256_round_trip(self, tmp_path: Path):
        """Full round-trip with SHA256 as primary digest."""
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        src = tmp_path / "src"
        src.mkdir()
        _write_files(src, {"file.txt": "hello"})
        stage = stage_dir(LocalFS(tmp_path), "src", DigestAlgorithm.SHA256)

        obj = root.commit(fsys, "sha256-obj", stage, created="2024-01-01T00:00:00Z")

        inv = obj.inventory.inventory
        assert inv.digest_algorithm == "sha256"

        # Sidecar uses sha256
        assert (tmp_path / "sha256-obj" / "inventory.json.sha256").exists()

        # Read back works
        with obj.open_logical_path(VNum(1), "file.txt") as f:
            assert f.read() == b"hello"

        result = ValidationResult()
        validate_object(fsys, "sha256-obj", result)
        assert result.is_valid


class TestDiffIntegration:
    """Diff between versions in a real object."""

    def test_diff_add_modify_delete_rename(self, tmp_path: Path):
        """Diff detects all change types across real versions."""
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        # v1
        src1 = tmp_path / "src1"
        src1.mkdir()
        _write_files(
            src1,
            {
                "keep.txt": "stable",
                "modify.txt": "original",
                "delete.txt": "going away",
                "rename-old.txt": "will be renamed",
            },
        )
        stage1 = stage_dir(LocalFS(tmp_path), "src1", DigestAlgorithm.SHA512)
        root.commit(fsys, "diff-obj", stage1, created="2024-01-01T00:00:00Z")

        # v2: keep same, modify changed, delete removed, rename-old → rename-new
        src2 = tmp_path / "src2"
        src2.mkdir()
        _write_files(
            src2,
            {
                "keep.txt": "stable",
                "modify.txt": "changed",
                "added.txt": "new file",
                "rename-new.txt": "will be renamed",  # same content as rename-old.txt
            },
        )
        stage2 = stage_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)
        obj = root.commit(fsys, "diff-obj", stage2, created="2024-06-01T00:00:00Z")

        diff = obj.diff_versions(VNum(1), VNum(2))
        actions = {e.path: e.action for e in diff}

        assert "added.txt" in actions
        assert actions["added.txt"] == "added"

        assert "modify.txt" in actions
        assert actions["modify.txt"] == "modified"

        # Rename detection: same content, different path
        # Should detect as rename
        rename_entries = [e for e in diff if e.action == "renamed"]
        assert len(rename_entries) == 1
        assert rename_entries[0].path == "rename-new.txt"
        assert rename_entries[0].renamed_from == "rename-old.txt"

        # delete.txt removed (not renamed since content is unique)
        delete_entries = [e for e in diff if e.action == "deleted"]
        assert any(e.path == "delete.txt" for e in delete_entries)

        # keep.txt not in diff (unchanged)
        assert "keep.txt" not in actions


class TestEdgeCases:
    """Integration edge cases."""

    def test_empty_version_state(self, tmp_path: Path):
        """Object version with no files."""
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        # v1 with files
        src1 = tmp_path / "src1"
        src1.mkdir()
        _write_files(src1, {"file.txt": "hello"})
        stage1 = stage_dir(LocalFS(tmp_path), "src1", DigestAlgorithm.SHA512)
        root.commit(fsys, "empty-obj", stage1, created="2024-01-01T00:00:00Z")

        # v2 with empty state
        src2 = tmp_path / "src2"
        src2.mkdir()
        stage2 = stage_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)
        obj = root.commit(fsys, "empty-obj", stage2, created="2024-06-01T00:00:00Z")

        assert obj.head == VNum(2)
        state = obj.version_state(VNum(2))
        assert len(state) == 0

        result = ValidationResult()
        validate_object(fsys, "empty-obj", result)
        assert result.is_valid

    def test_reopen_root_and_continue(self, tmp_path: Path):
        """Close and reopen storage root, then commit more."""
        root_dir = tmp_path / "root"
        root_dir.mkdir()
        fsys = LocalFS(root_dir)
        layout = HashedNTupleLayout()
        root = StorageRoot.init(fsys, fsys, layout=layout)

        src1 = tmp_path / "src1"
        src1.mkdir()
        _write_files(src1, {"file.txt": "v1"})
        stage1 = stage_dir(LocalFS(tmp_path), "src1", DigestAlgorithm.SHA512)
        root.commit(
            fsys,
            "http://example.org/reopen",
            stage1,
            created="2024-01-01T00:00:00Z",
        )

        # Reopen root from disk
        root2 = StorageRoot(fsys)
        assert root2.layout is not None

        # Add v2 via reopened root
        src2 = tmp_path / "src2"
        src2.mkdir()
        _write_files(src2, {"file.txt": "v2"})
        stage2 = stage_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)
        obj = root2.commit(
            fsys,
            "http://example.org/reopen",
            stage2,
            created="2024-06-01T00:00:00Z",
        )

        assert obj.head == VNum(2)
        with obj.open_logical_path(VNum(2), "file.txt") as f:
            assert f.read() == b"v2"

        result = ValidationResult()
        validate_root(fsys, "", result)
        assert result.is_valid

    def test_large_file_count(self, tmp_path: Path):
        """Object with many files stages and validates correctly."""
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        src = tmp_path / "src"
        src.mkdir()
        files = {f"file-{i:03d}.txt": f"content-{i}" for i in range(50)}
        _write_files(src, files)
        stage = stage_dir(LocalFS(tmp_path), "src", DigestAlgorithm.SHA512)

        obj = root.commit(fsys, "many-files", stage, created="2024-01-01T00:00:00Z")

        state = obj.version_state(VNum(1))
        all_paths = sorted(p for ps in state.values() for p in ps)
        assert len(all_paths) == 50

        result = ValidationResult()
        validate_object(fsys, "many-files", result)
        assert result.is_valid

    def test_binary_content(self, tmp_path: Path):
        """Non-UTF8 binary content round-trips correctly."""
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        src = tmp_path / "src"
        src.mkdir()
        binary_data = bytes(range(256))
        (src / "binary.bin").write_bytes(binary_data)
        stage = stage_dir(LocalFS(tmp_path), "src", DigestAlgorithm.SHA512)

        obj = root.commit(fsys, "bin-obj", stage, created="2024-01-01T00:00:00Z")

        with obj.open_logical_path(VNum(1), "binary.bin") as f:
            assert f.read() == binary_data

        result = ValidationResult()
        validate_object(fsys, "bin-obj", result)
        assert result.is_valid

    def test_duplicate_content_dedup(self, tmp_path: Path):
        """Two files with identical content share one manifest entry."""
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        src = tmp_path / "src"
        src.mkdir()
        _write_files(src, {"a.txt": "same", "b.txt": "same"})
        stage = stage_dir(LocalFS(tmp_path), "src", DigestAlgorithm.SHA512)

        obj = root.commit(fsys, "dedup-obj", stage, created="2024-01-01T00:00:00Z")

        inv = obj.inventory.inventory
        # One digest in manifest maps to one content path
        assert len(inv.manifest) == 1
        content_paths = next(iter(inv.manifest.values()))
        assert len(content_paths) == 1

        # State maps that digest to both logical paths
        state = obj.version_state(VNum(1))
        assert len(state) == 1
        logical_paths = sorted(next(iter(state.values())))
        assert logical_paths == ["a.txt", "b.txt"]

        result = ValidationResult()
        validate_object(fsys, "dedup-obj", result)
        assert result.is_valid

    def test_non_default_layout_config(self, tmp_path: Path):
        """Layout with non-default config round-trips through disk."""
        layout = HashedNTupleLayout(tuple_size=2, number_of_tuples=2)
        root_dir = tmp_path / "root"
        root_dir.mkdir()
        fsys = LocalFS(root_dir)
        StorageRoot.init(fsys, fsys, layout=layout)

        # Reopen and verify config persisted
        root2 = StorageRoot(fsys)
        assert root2.layout is not None
        cfg = root2.layout.config
        assert cfg is not None
        assert cfg["tupleSize"] == 2
        assert cfg["numberOfTuples"] == 2

        # Commit and verify object resolves to same path
        src = tmp_path / "src"
        src.mkdir()
        _write_files(src, {"file.txt": "content"})
        stage = stage_dir(LocalFS(tmp_path), "src", DigestAlgorithm.SHA512)
        obj = root2.commit(
            fsys,
            "http://example.org/cfg-test",
            stage,
            created="2024-01-01T00:00:00Z",
        )
        assert obj.exists

        result = ValidationResult()
        validate_root(fsys, "", result)
        assert result.is_valid

    def test_objects_batch(self, tmp_path: Path):
        """objects_batch() returns same set as objects()."""
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        for i in range(5):
            src = tmp_path / f"src-{i}"
            src.mkdir()
            _write_files(src, {"data.txt": f"content-{i}"})
            stage = stage_dir(
                LocalFS(tmp_path),
                f"src-{i}",
                DigestAlgorithm.SHA512,
            )
            root.commit(
                fsys,
                f"obj-{i}",
                stage,
                created="2024-01-01T00:00:00Z",
            )

        serial_ids = sorted(o.id for o in root.objects())
        batch_ids = sorted(o.id for o in root.objects_batch())
        assert serial_ids == batch_ids

    def test_cold_read_via_get_object(self, tmp_path: Path):
        """Read object via get_object without prior commit."""
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        src = tmp_path / "src"
        src.mkdir()
        _write_files(src, {"file.txt": "hello"})
        stage = stage_dir(LocalFS(tmp_path), "src", DigestAlgorithm.SHA512)
        root.commit(fsys, "cold-obj", stage, created="2024-01-01T00:00:00Z")

        # Fresh root instance, pure read path
        root2 = StorageRoot(fsys)
        obj = root2.get_object("cold-obj")
        assert obj.id == "cold-obj"
        with obj.open_logical_path(VNum(1), "file.txt") as f:
            assert f.read() == b"hello"
