"""Tests for StorageRoot.purge()."""

from typing import TYPE_CHECKING

import pytest

from ocfl_lib._digest import DigestAlgorithm
from ocfl_lib._errors import LayoutNotConfigured, ObjectNotFound
from ocfl_lib._layout import FlatDirectLayout, HashedNTupleLayout
from ocfl_lib._root import StorageRoot
from ocfl_lib._version_state import scan_dir
from ocfl_lib.fs._local import LocalFS

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
    stage = scan_dir(LocalFS(src.parent), src.name, DigestAlgorithm.SHA512)
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

    def test_purge_preserves_non_empty_shared_parent(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        layout = HashedNTupleLayout(tuple_size=1, number_of_tuples=1)
        root = StorageRoot.init(fsys, fsys, layout=layout)

        ids_by_parent: dict[str, list[str]] = {}
        for i in range(64):
            obj_id = f"obj-{i}"
            parent = root.object_path(obj_id).split("/", 1)[0]
            ids_by_parent.setdefault(parent, []).append(obj_id)
            if len(ids_by_parent[parent]) == 2:
                obj_a, obj_b = ids_by_parent[parent]
                break
        else:  # pragma: no cover - impossible with 64 IDs into 16 hex buckets
            raise AssertionError("could not find IDs with a shared parent")

        _commit_object(tmp_path, root, fsys, obj_a, {"file.txt": "a"})
        _commit_object(tmp_path, root, fsys, obj_b, {"file.txt": "b"})
        obj_a_path = root.object_path(obj_a)
        obj_b_path = root.object_path(obj_b)
        shared_parent = obj_a_path.split("/", 1)[0]

        root.purge(fsys, obj_a)

        assert not (tmp_path / obj_a_path).exists()
        assert (tmp_path / shared_parent).is_dir()
        assert (tmp_path / obj_b_path).exists()
        assert root.get_object(obj_b).exists

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
