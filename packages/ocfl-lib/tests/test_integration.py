import hashlib
import json
from typing import TYPE_CHECKING

import pytest
from builders import stage_files, write_tree

from ocfl_lib._digest import DigestAlgorithm
from ocfl_lib._inventory import UserInfo
from ocfl_lib._layout import (
    FlatDirectLayout,
    FlatOmitPrefixLayout,
    HashAndIdNTupleLayout,
    HashedNTupleLayout,
    NTupleOmitPrefixLayout,
)
from ocfl_lib._root import StorageRoot
from ocfl_lib._spec import Spec, VNum
from ocfl_lib._version_state import scan_dir
from ocfl_lib.fs._local import LocalFS
from ocfl_lib.validation._object import validate_object
from ocfl_lib.validation._root import validate_root

if TYPE_CHECKING:
    from pathlib import Path

    from ocfl_lib._layout.types import Layout


_OBJECT_ID_BY_LAYOUT = {
    "flat-direct": "obj",
    "hashed-n-tuple": "http://example.org/obj",
    "hash-and-id-n-tuple": "http://example.org/obj",
    "flat-omit-prefix": "ns:obj",
    "n-tuple-omit-prefix": "ns:obj",
}


def _make_layout(name: str) -> Layout:
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
    @pytest.mark.parametrize("layout_name", ALL_LAYOUTS)
    def test_create_commit_validate_read(self, tmp_path: Path, layout_name: str):
        layout = _make_layout(layout_name)
        obj_id = _OBJECT_ID_BY_LAYOUT[layout_name]

        root_dir = tmp_path / "root"
        root_dir.mkdir()
        fsys = LocalFS(root_dir)
        root = StorageRoot.init(fsys, fsys, layout=layout)

        stage1 = stage_files(
            tmp_path, {"hello.txt": "hello", "dir/nested.txt": "nested"}, name="src1"
        )
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

        stage2 = stage_files(
            tmp_path,
            {"hello.txt": "hello modified", "new.txt": "brand new"},
            name="src2",
        )
        obj = root.commit(
            fsys,
            obj_id,
            stage2,
            message="second version",
            user=UserInfo(name="Bob"),
            created="2024-06-01T00:00:00Z",
        )

        assert obj.head == VNum(2)

        summary = result = validate_root(fsys, "")
        assert result.is_valid, f"errors: {[e.render() for e in result.errors]}"
        assert summary.objects_checked == 1
        assert summary.objects_valid == 1

        with obj.open_logical_path(VNum(1), "hello.txt") as f:
            assert f.read() == b"hello"
        with obj.open_logical_path(VNum(1), "dir/nested.txt") as f:
            assert f.read() == b"nested"

        with obj.open_logical_path(VNum(2), "hello.txt") as f:
            assert f.read() == b"hello modified"
        with obj.open_logical_path(VNum(2), "new.txt") as f:
            assert f.read() == b"brand new"

        with pytest.raises(KeyError, match=r"dir/nested\.txt"):
            obj.open_logical_path(VNum(2), "dir/nested.txt")

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
        layout = _make_layout(layout_name)
        root_dir = tmp_path / "root"
        root_dir.mkdir()
        fsys = LocalFS(root_dir)
        root = StorageRoot.init(fsys, fsys, layout=layout)

        base = _OBJECT_ID_BY_LAYOUT[layout_name]
        ids = [f"{base}-{i}" for i in range(3)]

        for i, oid in enumerate(ids):
            stage = stage_files(tmp_path, {"data.txt": f"content-{i}"}, name=f"src-{i}")
            root.commit(fsys, oid, stage, created="2024-01-01T00:00:00Z")

        found_ids = sorted(o.id for o in root.objects())
        assert found_ids == sorted(ids)

        summary = result = validate_root(fsys, "")
        assert result.is_valid
        assert summary.objects_checked == 3
        assert summary.objects_valid == 3


class TestFixityRoundTrip:
    def test_fixity_single_algorithm(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        write_tree(tmp_path / "src", {"a.txt": "aaa", "b.txt": "bbb"})
        stage = scan_dir(
            LocalFS(tmp_path),
            "src",
            DigestAlgorithm.SHA512,
            fixity_algs=[DigestAlgorithm.SHA256],
        )

        obj = root.commit(fsys, "fix-obj", stage, created="2024-01-01T00:00:00Z")

        inv = obj.inventory.inventory
        assert inv.fixity is not None
        assert "sha256" in inv.fixity
        sha256_paths = sorted(p for ps in inv.fixity["sha256"].values() for p in ps)
        manifest_paths = sorted(p for ps in inv.manifest.values() for p in ps)
        assert sha256_paths == manifest_paths

        result = validate_object(fsys, "fix-obj")
        assert result.is_valid

    def test_fixity_multiple_algorithms(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        write_tree(tmp_path / "src", {"file.txt": "content"})
        stage = scan_dir(
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

        result = validate_object(fsys, "multi-fix")
        assert result.is_valid

    def test_fixity_preserved_across_versions(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        write_tree(tmp_path / "src1", {"keep.txt": "stable", "change.txt": "old"})
        stage1 = scan_dir(
            LocalFS(tmp_path),
            "src1",
            DigestAlgorithm.SHA512,
            fixity_algs=[DigestAlgorithm.SHA256],
        )
        root.commit(fsys, "fix-v2", stage1, created="2024-01-01T00:00:00Z")

        write_tree(tmp_path / "src2", {"keep.txt": "stable", "change.txt": "new"})
        stage2 = scan_dir(
            LocalFS(tmp_path),
            "src2",
            DigestAlgorithm.SHA512,
            fixity_algs=[DigestAlgorithm.SHA256],
        )
        obj = root.commit(fsys, "fix-v2", stage2, created="2024-06-01T00:00:00Z")

        inv = obj.inventory.inventory
        assert inv.fixity is not None
        assert "sha256" in inv.fixity
        all_manifest_paths = sorted(p for ps in inv.manifest.values() for p in ps)
        all_fixity_paths = sorted(p for ps in inv.fixity["sha256"].values() for p in ps)
        assert all_fixity_paths == all_manifest_paths

        result = validate_object(fsys, "fix-v2")
        assert result.is_valid


class TestOnDiskStructure:
    def test_object_structure_v1(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        stage = stage_files(tmp_path, {"file.txt": "hello"})
        root.commit(fsys, "struct-obj", stage, created="2024-01-01T00:00:00Z")

        obj_dir = tmp_path / "struct-obj"
        assert obj_dir.is_dir()

        assert (obj_dir / "0=ocfl_object_1.1").exists()
        assert (obj_dir / "0=ocfl_object_1.1").read_text() == "ocfl_object_1.1\n"

        assert (obj_dir / "inventory.json").exists()
        assert (obj_dir / "inventory.json.sha512").exists()

        v1_dir = obj_dir / "v1"
        assert v1_dir.is_dir()

        assert (v1_dir / "inventory.json").exists()
        assert (v1_dir / "inventory.json.sha512").exists()

        content_dir = v1_dir / "content"
        assert content_dir.is_dir()
        assert (content_dir / "file.txt").exists()
        assert (content_dir / "file.txt").read_text() == "hello"

        assert (obj_dir / "inventory.json").read_bytes() == (
            v1_dir / "inventory.json"
        ).read_bytes()

        inventory_digest = hashlib.sha512(
            (obj_dir / "inventory.json").read_bytes()
        ).hexdigest()
        assert (obj_dir / "inventory.json.sha512").read_text() == (
            f"{inventory_digest} inventory.json\n"
        )

    def test_object_structure_v2_forward_delta(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        stage1 = stage_files(
            tmp_path, {"keep.txt": "stable", "change.txt": "old"}, name="src1"
        )
        root.commit(fsys, "delta-obj", stage1, created="2024-01-01T00:00:00Z")

        stage2 = stage_files(
            tmp_path, {"keep.txt": "stable", "change.txt": "new"}, name="src2"
        )
        root.commit(fsys, "delta-obj", stage2, created="2024-06-01T00:00:00Z")

        obj_dir = tmp_path / "delta-obj"

        v1_content = obj_dir / "v1" / "content"
        assert (v1_content / "keep.txt").exists()
        assert (v1_content / "change.txt").exists()

        v2_content = obj_dir / "v2" / "content"
        assert v2_content.is_dir()
        v2_files = sorted(p.name for p in v2_content.rglob("*") if p.is_file())
        assert v2_files == ["change.txt"]

        assert (obj_dir / "v2" / "inventory.json").exists()

        root_inv = json.loads((obj_dir / "inventory.json").read_bytes())
        assert root_inv["head"] == "v2"

    def test_root_structure(self, tmp_path: Path):
        layout = HashedNTupleLayout()
        fsys = LocalFS(tmp_path)
        StorageRoot.init(fsys, fsys, layout=layout, layout_description="test layout")

        assert (tmp_path / "0=ocfl_1.1").exists()

        layout_data = json.loads((tmp_path / "ocfl_layout.json").read_bytes())
        assert layout_data["extension"] == "0004-hashed-n-tuple-storage-layout"
        assert layout_data["description"] == "test layout"

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
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        stage = stage_files(tmp_path, {"file.txt": "content"})
        root.commit(
            fsys,
            "inv-test",
            stage,
            message="test",
            user=UserInfo(name="Tester", address="mailto:test@example.org"),
            created="2024-01-01T00:00:00Z",
        )

        inv = json.loads((tmp_path / "inv-test" / "inventory.json").read_bytes())

        assert inv["id"] == "inv-test"
        assert inv["type"] == "https://ocfl.io/1.1/spec/#inventory"
        assert inv["digestAlgorithm"] == "sha512"
        assert inv["head"] == "v1"
        assert isinstance(inv["manifest"], dict)
        assert isinstance(inv["versions"], dict)

        assert "digest_algorithm" not in inv
        assert "content_directory" not in inv

        v1 = inv["versions"]["v1"]
        assert v1["created"] == "2024-01-01T00:00:00Z"
        assert v1["message"] == "test"
        assert v1["user"]["name"] == "Tester"
        assert v1["user"]["address"] == "mailto:test@example.org"
        assert isinstance(v1["state"], dict)


class TestSpecVersions:
    @pytest.mark.parametrize("spec", [Spec.V1_0, Spec.V1_1])
    def test_spec_round_trip(self, tmp_path: Path, spec: Spec):
        root_dir = tmp_path / "root"
        root_dir.mkdir()
        fsys = LocalFS(root_dir)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout(), spec=spec)
        assert root.spec == spec

        stage = stage_files(tmp_path, {"file.txt": "content"})
        obj = root.commit(fsys, "spec-obj", stage, created="2024-01-01T00:00:00Z")

        assert obj.spec == spec

        result = validate_root(fsys, "")
        assert result.is_valid

        expected_namaste = f"0=ocfl_{spec.value}"
        assert (root_dir / expected_namaste).exists()

    def test_sha256_round_trip(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        stage = stage_files(tmp_path, {"file.txt": "hello"}, alg=DigestAlgorithm.SHA256)

        obj = root.commit(fsys, "sha256-obj", stage, created="2024-01-01T00:00:00Z")

        inv = obj.inventory.inventory
        assert inv.digest_algorithm == "sha256"

        assert (tmp_path / "sha256-obj" / "inventory.json.sha256").exists()

        with obj.open_logical_path(VNum(1), "file.txt") as f:
            assert f.read() == b"hello"

        result = validate_object(fsys, "sha256-obj")
        assert result.is_valid


class TestDiffIntegration:
    def test_diff_add_modify_delete_rename(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        stage1 = stage_files(
            tmp_path,
            {
                "keep.txt": "stable",
                "modify.txt": "original",
                "delete.txt": "going away",
                "rename-old.txt": "will be renamed",
            },
            name="src1",
        )
        root.commit(fsys, "diff-obj", stage1, created="2024-01-01T00:00:00Z")

        stage2 = stage_files(
            tmp_path,
            {
                "keep.txt": "stable",
                "modify.txt": "changed",
                "added.txt": "new file",
                "rename-new.txt": "will be renamed",
            },
            name="src2",
        )
        obj = root.commit(fsys, "diff-obj", stage2, created="2024-06-01T00:00:00Z")

        diff = obj.diff_versions(VNum(1), VNum(2))
        actions = {e.path: e.action for e in diff}

        assert "added.txt" in actions
        assert actions["added.txt"] == "added"

        assert "modify.txt" in actions
        assert actions["modify.txt"] == "modified"

        rename_entries = [e for e in diff if e.action == "renamed"]
        assert len(rename_entries) == 1
        assert rename_entries[0].path == "rename-new.txt"
        assert rename_entries[0].renamed_from == "rename-old.txt"

        delete_entries = [e for e in diff if e.action == "deleted"]
        assert any(e.path == "delete.txt" for e in delete_entries)

        assert "keep.txt" not in actions


class TestEdgeCases:
    def test_empty_version_state(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        stage1 = stage_files(tmp_path, {"file.txt": "hello"}, name="src1")
        root.commit(fsys, "empty-obj", stage1, created="2024-01-01T00:00:00Z")

        stage2 = stage_files(tmp_path, {}, name="src2")
        obj = root.commit(fsys, "empty-obj", stage2, created="2024-06-01T00:00:00Z")

        assert obj.head == VNum(2)
        state = obj.version_info(VNum(2)).state
        assert len(state) == 0

        result = validate_object(fsys, "empty-obj")
        assert result.is_valid

    def test_reopen_root_and_continue(self, tmp_path: Path):
        root_dir = tmp_path / "root"
        root_dir.mkdir()
        fsys = LocalFS(root_dir)
        layout = HashedNTupleLayout()
        root = StorageRoot.init(fsys, fsys, layout=layout)

        stage1 = stage_files(tmp_path, {"file.txt": "v1"}, name="src1")
        root.commit(
            fsys,
            "http://example.org/reopen",
            stage1,
            created="2024-01-01T00:00:00Z",
        )

        root2 = StorageRoot.open(fsys)
        assert root2.layout is not None

        stage2 = stage_files(tmp_path, {"file.txt": "v2"}, name="src2")
        obj = root2.commit(
            fsys,
            "http://example.org/reopen",
            stage2,
            created="2024-06-01T00:00:00Z",
        )

        assert obj.head == VNum(2)
        with obj.open_logical_path(VNum(2), "file.txt") as f:
            assert f.read() == b"v2"

        result = validate_root(fsys, "")
        assert result.is_valid

    def test_large_file_count(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        files = {f"file-{i:03d}.txt": f"content-{i}" for i in range(50)}
        stage = stage_files(tmp_path, files)

        obj = root.commit(fsys, "many-files", stage, created="2024-01-01T00:00:00Z")

        state = obj.version_info(VNum(1)).state
        all_paths = sorted(p for ps in state.values() for p in ps)
        assert len(all_paths) == 50

        result = validate_object(fsys, "many-files")
        assert result.is_valid

    def test_binary_content(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        src = tmp_path / "src"
        src.mkdir()
        binary_data = bytes(range(256))
        (src / "binary.bin").write_bytes(binary_data)
        stage = scan_dir(LocalFS(tmp_path), "src", DigestAlgorithm.SHA512)

        obj = root.commit(fsys, "bin-obj", stage, created="2024-01-01T00:00:00Z")

        with obj.open_logical_path(VNum(1), "binary.bin") as f:
            assert f.read() == binary_data

        result = validate_object(fsys, "bin-obj")
        assert result.is_valid

    def test_duplicate_content_dedup(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        stage = stage_files(tmp_path, {"a.txt": "same", "b.txt": "same"})

        obj = root.commit(fsys, "dedup-obj", stage, created="2024-01-01T00:00:00Z")

        inv = obj.inventory.inventory
        assert len(inv.manifest) == 1
        content_paths = next(iter(inv.manifest.values()))
        assert len(content_paths) == 1

        state = obj.version_info(VNum(1)).state
        assert len(state) == 1
        logical_paths = sorted(next(iter(state.values())))
        assert logical_paths == ["a.txt", "b.txt"]

        result = validate_object(fsys, "dedup-obj")
        assert result.is_valid

    def test_non_default_layout_config(self, tmp_path: Path):
        layout = HashedNTupleLayout(tuple_size=2, number_of_tuples=2)
        root_dir = tmp_path / "root"
        root_dir.mkdir()
        fsys = LocalFS(root_dir)
        StorageRoot.init(fsys, fsys, layout=layout)

        root2 = StorageRoot.open(fsys)
        assert root2.layout is not None
        cfg = root2.layout.config
        assert cfg is not None
        assert cfg["tupleSize"] == 2
        assert cfg["numberOfTuples"] == 2

        stage = stage_files(tmp_path, {"file.txt": "content"})
        obj = root2.commit(
            fsys,
            "http://example.org/cfg-test",
            stage,
            created="2024-01-01T00:00:00Z",
        )
        assert obj.exists

        result = validate_root(fsys, "")
        assert result.is_valid

    def test_objects_batch(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        for i in range(5):
            stage = stage_files(tmp_path, {"data.txt": f"content-{i}"}, name=f"src-{i}")
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
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        stage = stage_files(tmp_path, {"file.txt": "hello"})
        root.commit(fsys, "cold-obj", stage, created="2024-01-01T00:00:00Z")

        root2 = StorageRoot.open(fsys)
        obj = root2.get_object("cold-obj")
        assert obj.id == "cold-obj"
        with obj.open_logical_path(VNum(1), "file.txt") as f:
            assert f.read() == b"hello"
