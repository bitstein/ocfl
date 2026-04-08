"""Tests for StorageRoot.purge()."""

from typing import TYPE_CHECKING

import pytest

from ocfl_lib.digest import DigestAlgorithm
from ocfl_lib.errors import LayoutNotConfigured, ObjectNotFound
from ocfl_lib.fs.local import LocalFS
from ocfl_lib.layout import FlatDirectLayout, HashedNTupleLayout
from ocfl_lib.root import StorageRoot
from ocfl_lib.stage import stage_dir

if TYPE_CHECKING:
    from pathlib import Path


def _commit_object(
    tmp_path: Path, root: StorageRoot, fsys: LocalFS, obj_id: str, files: dict[str, str]
) -> None:
    src = tmp_path / f"src-{obj_id.replace(':', '-')}"
    src.mkdir(exist_ok=True)
    for name, content in files.items():
        p = src / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    stage = stage_dir(LocalFS(src.parent), src.name, DigestAlgorithm.SHA512)
    root.commit(fsys, obj_id, stage, created="2024-01-01T00:00:00Z")


class TestStorageRootPurge:
    def test_purge_removes_object(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())
        _commit_object(tmp_path, root, fsys, "obj-a", {"file.txt": "hello"})

        root.purge(fsys, "obj-a")

        # Object gone
        obj = root.get_object("obj-a")
        assert not obj.exists
        assert not (tmp_path / "obj-a").exists()

    def test_purge_leaves_other_objects(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())
        _commit_object(tmp_path, root, fsys, "obj-a", {"file.txt": "a"})
        _commit_object(tmp_path, root, fsys, "obj-b", {"file.txt": "b"})

        root.purge(fsys, "obj-a")

        assert not root.get_object("obj-a").exists
        assert root.get_object("obj-b").exists

    def test_purge_cleans_empty_parents(self, tmp_path: Path):
        """Hashed layout creates nested dirs; purge should clean empty parents."""
        fsys = LocalFS(tmp_path)
        layout = HashedNTupleLayout()
        root = StorageRoot.init(fsys, fsys, layout=layout)
        _commit_object(tmp_path, root, fsys, "test:purge", {"file.txt": "x"})

        obj_path = root.object_path("test:purge")
        # Verify nested dirs exist
        assert (tmp_path / obj_path).exists()

        root.purge(fsys, "test:purge")

        # Object dir gone
        assert not (tmp_path / obj_path).exists()
        # Parent tuple dirs should be cleaned up too
        parts = obj_path.split("/")
        for i in range(len(parts)):
            partial = "/".join(parts[: i + 1])
            assert not (tmp_path / partial).exists()

    def test_purge_nonexistent_raises(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())

        with pytest.raises(ObjectNotFound, match="not found"):
            root.purge(fsys, "no-such-object")

    def test_purge_no_layout_raises(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys)

        with pytest.raises(LayoutNotConfigured, match="no layout"):
            root.purge(fsys, "obj-a")
