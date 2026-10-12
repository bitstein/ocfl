"""Tests for StorageRoot write operations."""

import json
from typing import TYPE_CHECKING

import pytest

from ocfl_lib.digest import DigestAlgorithm
from ocfl_lib.errors import LayoutNotConfigured
from ocfl_lib.fs.local import LocalFS
from ocfl_lib.inventory import UserInfo
from ocfl_lib.layout import FlatDirectLayout, HashedNTupleLayout
from ocfl_lib.root import StorageRoot
from ocfl_lib.spec import Spec, VNum
from ocfl_lib.validation.object import validate_object
from ocfl_lib.validation.result import ValidationResult
from ocfl_lib.version_state import scan_dir

if TYPE_CHECKING:
    from pathlib import Path


def _make_src(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create source directory with given files."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    for name, content in files.items():
        p = src / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return src


class TestStorageRootInit:
    """StorageRoot.init() — create new root."""

    def test_init_default_spec(self, tmp_path: Path):
        """Init with default spec (1.1), no layout."""
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys)
        assert root.spec == Spec.V1_1

    def test_init_spec_1_0(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, spec=Spec.V1_0)
        assert root.spec == Spec.V1_0

    def test_init_writes_namaste(self, tmp_path: Path):
        """NAMASTE declaration file exists on disk."""
        fsys = LocalFS(tmp_path)
        StorageRoot.init(fsys, fsys)
        assert (tmp_path / "0=ocfl_1.1").exists()
        assert (tmp_path / "0=ocfl_1.1").read_text() == "ocfl_1.1\n"

    def test_init_with_layout(self, tmp_path: Path):
        """Init with layout writes ocfl_layout.json + extension config."""
        layout = HashedNTupleLayout()
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=layout)

        assert root.layout is not None
        assert root.layout.name == "0004-hashed-n-tuple-storage-layout"
        assert (tmp_path / "ocfl_layout.json").exists()
        assert (
            tmp_path
            / "extensions"
            / "0004-hashed-n-tuple-storage-layout"
            / "config.json"
        ).exists()

    def test_init_with_layout_description(self, tmp_path: Path):
        """Layout description written to ocfl_layout.json."""
        layout = FlatDirectLayout()
        fsys = LocalFS(tmp_path)
        StorageRoot.init(fsys, fsys, layout=layout, layout_description="flat direct")

        data = json.loads((tmp_path / "ocfl_layout.json").read_text())
        assert data["description"] == "flat direct"
        assert data["extension"] == "0002-flat-direct-storage-layout"

    def test_init_layout_no_config(self, tmp_path: Path):
        """Layout with config=None doesn't write extension config dir."""
        layout = FlatDirectLayout()
        fsys = LocalFS(tmp_path)
        StorageRoot.init(fsys, fsys, layout=layout)

        assert (tmp_path / "ocfl_layout.json").exists()
        # FlatDirect has config=None, so no extension config dir
        assert not (
            tmp_path / "extensions" / "0002-flat-direct-storage-layout" / "config.json"
        ).exists()

    def test_init_reopen(self, tmp_path: Path):
        """Initialized root can be re-opened as StorageRoot."""
        layout = HashedNTupleLayout()
        fsys = LocalFS(tmp_path)
        StorageRoot.init(fsys, fsys, layout=layout)

        # Re-open from disk
        root2 = StorageRoot(fsys)
        assert root2.spec == Spec.V1_1
        assert root2.layout is not None
        assert root2.layout.name == "0004-hashed-n-tuple-storage-layout"

    def test_init_existing_root_raises(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        StorageRoot.init(fsys, fsys, layout=HashedNTupleLayout())

        with pytest.raises(ValueError, match="already initialized"):
            StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        data = json.loads((tmp_path / "ocfl_layout.json").read_text())
        assert data["extension"] == "0004-hashed-n-tuple-storage-layout"

    def test_init_with_subpath(self, tmp_path: Path):
        """Init at a sub-path within the filesystem."""
        (tmp_path / "sub").mkdir()
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, path="sub")
        assert root.spec == Spec.V1_1
        assert (tmp_path / "sub" / "0=ocfl_1.1").exists()

    def test_init_no_objects_initially(self, tmp_path: Path):
        """Freshly initialized root has no objects."""
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys)
        assert list(root.object_paths()) == []


class TestStorageRootCommit:
    """StorageRoot.commit() — add/update objects."""

    def test_commit_new_object(self, tmp_path: Path):
        """Commit new object to root, returns Object."""
        src = _make_src(tmp_path, {"file.txt": "hello"})
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        src_fs = LocalFS(src.parent)
        stage = scan_dir(src_fs, "src", DigestAlgorithm.SHA512)

        obj = root.commit(
            fsys,
            "my-object",
            stage,
            message="initial",
            user=UserInfo(name="Test"),
            created="2024-01-01T00:00:00Z",
        )

        assert obj.id == "my-object"
        assert obj.head == VNum(1)
        assert obj.spec == Spec.V1_1

    def test_commit_validates(self, tmp_path: Path):
        """Committed object passes validation."""
        src = _make_src(tmp_path, {"file.txt": "hello"})
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        src_fs = LocalFS(src.parent)
        stage = scan_dir(src_fs, "src", DigestAlgorithm.SHA512)

        root.commit(fsys, "my-obj", stage, created="2024-01-01T00:00:00Z")

        result = ValidationResult()
        validate_object(fsys, "my-obj", result)
        assert result.is_valid, f"errors: {[e.message for e in result.errors]}"

    def test_commit_update_existing(self, tmp_path: Path):
        """Commit to same ID creates v2."""
        src1 = _make_src(tmp_path, {"file.txt": "v1"})
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        src_fs = LocalFS(src1.parent)
        stage1 = scan_dir(src_fs, "src", DigestAlgorithm.SHA512)
        root.commit(fsys, "my-obj", stage1, created="2024-01-01T00:00:00Z")

        # v2
        src2 = tmp_path / "src2"
        src2.mkdir()
        (src2 / "file.txt").write_text("v2")
        stage2 = scan_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)

        obj = root.commit(
            fsys, "my-obj", stage2, created="2024-06-01T00:00:00Z", message="v2"
        )
        assert obj.head == VNum(2)

    def test_commit_readable_via_get_object(self, tmp_path: Path):
        """After commit, get_object returns accessible object."""
        src = _make_src(tmp_path, {"file.txt": "hello"})
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        src_fs = LocalFS(src.parent)
        stage = scan_dir(src_fs, "src", DigestAlgorithm.SHA512)
        root.commit(fsys, "my-obj", stage, created="2024-01-01T00:00:00Z")

        obj = root.get_object("my-obj")
        assert obj.id == "my-obj"
        with obj.open_logical_path(VNum(1), "file.txt") as f:
            assert f.read() == b"hello"

    def test_commit_no_layout_raises(self, tmp_path: Path):
        """Commit without layout raises ValueError."""
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys)

        src = _make_src(tmp_path, {"file.txt": "x"})
        stage = scan_dir(LocalFS(src.parent), "src", DigestAlgorithm.SHA512)

        with pytest.raises(LayoutNotConfigured, match="no layout"):
            root.commit(fsys, "obj", stage, created="2024-01-01T00:00:00Z")

    def test_commit_defaults_spec_to_root(self, tmp_path: Path):
        """New object spec defaults to root's spec."""
        src = _make_src(tmp_path, {"file.txt": "x"})
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout(), spec=Spec.V1_0)

        src_fs = LocalFS(src.parent)
        stage = scan_dir(src_fs, "src", DigestAlgorithm.SHA512)
        obj = root.commit(fsys, "my-obj", stage, created="2024-01-01T00:00:00Z")
        assert obj.spec == Spec.V1_0

    def test_commit_with_hashed_layout(self, tmp_path: Path):
        """Commit with hashed n-tuple layout resolves path correctly."""
        src = _make_src(tmp_path, {"file.txt": "hello"})
        fsys = LocalFS(tmp_path)
        layout = HashedNTupleLayout()
        root = StorageRoot.init(fsys, fsys, layout=layout)

        src_fs = LocalFS(src.parent)
        stage = scan_dir(src_fs, "src", DigestAlgorithm.SHA512)
        obj = root.commit(fsys, "test:obj", stage, created="2024-01-01T00:00:00Z")

        assert obj.id == "test:obj"
        # Verify stored at hashed path
        expected_path = root.object_path("test:obj")
        assert (tmp_path / expected_path / "0=ocfl_object_1.1").exists()

    def test_commit_objects_iterable(self, tmp_path: Path):
        """After commits, objects() yields all committed objects."""
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        for name in ["obj-a", "obj-b"]:
            src = tmp_path / f"src-{name}"
            src.mkdir()
            (src / "file.txt").write_text(name)
            stage = scan_dir(LocalFS(tmp_path), f"src-{name}", DigestAlgorithm.SHA512)
            root.commit(fsys, name, stage, created="2024-01-01T00:00:00Z")

        ids = sorted(o.id for o in root.objects())
        assert ids == ["obj-a", "obj-b"]
