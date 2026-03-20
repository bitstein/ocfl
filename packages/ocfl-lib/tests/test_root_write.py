"""Tests for StorageRoot write operations (Phase 12.1-12.2)."""

from typing import TYPE_CHECKING

from ocfl_lib.fs.local import LocalFS
from ocfl_lib.layout import FlatDirectLayout, HashedNTupleLayout
from ocfl_lib.root import StorageRoot
from ocfl_lib.spec import Spec

if TYPE_CHECKING:
    from pathlib import Path


class TestStorageRootInit:
    """12.1: StorageRoot.init() — create new root."""

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
        import json

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
