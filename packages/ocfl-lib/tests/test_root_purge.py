from typing import TYPE_CHECKING

import pytest
from builders import Died, stage_files

from ocfl_lib._errors import (
    LayoutNotConfigured,
    ObjectLockedError,
    ObjectNotFound,
    RecoveryRequiredError,
)
from ocfl_lib._layout import FlatDirectLayout, HashedNTupleLayout
from ocfl_lib._root import StorageRoot
from ocfl_lib.fs._local import LocalFS

if TYPE_CHECKING:
    from pathlib import Path


def _commit_object(
    tmp_path: Path, root: StorageRoot, fsys: LocalFS, obj_id: str, files: dict[str, str]
) -> None:
    stage = stage_files(tmp_path, files, name=f"src-{obj_id.replace(':', '-')}")
    root.commit(fsys, obj_id, stage, created="2024-01-01T00:00:00Z")


class TestStorageRootPurge:
    def test_purge_removes_object(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())
        _commit_object(tmp_path, root, fsys, "obj-a", {"file.txt": "hello"})

        root.purge(fsys, "obj-a")

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
        fsys = LocalFS(tmp_path)
        layout = HashedNTupleLayout()
        root = StorageRoot.init(fsys, fsys, layout=layout)
        _commit_object(tmp_path, root, fsys, "test:purge", {"file.txt": "x"})

        obj_path = root.object_path("test:purge")
        assert (tmp_path / obj_path).exists()

        root.purge(fsys, "test:purge")

        assert not (tmp_path / obj_path).exists()
        parts = obj_path.split("/")
        for i in range(len(parts)):
            partial = "/".join(parts[: i + 1])
            assert not (tmp_path / partial).exists()

    def test_purge_preserves_non_empty_shared_parent(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        layout = HashedNTupleLayout(tuple_size=1, number_of_tuples=1)
        root = StorageRoot.init(fsys, fsys, layout=layout)

        hex_buckets = 16
        ids_by_parent: dict[str, list[str]] = {}
        for i in range(hex_buckets + 1):
            obj_id = f"obj-{i}"
            parent = root.object_path(obj_id).split("/", 1)[0]
            ids_by_parent.setdefault(parent, []).append(obj_id)
        obj_a, obj_b, *_ = next(ids for ids in ids_by_parent.values() if len(ids) >= 2)

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


class _Recorder(LocalFS):
    def __init__(self, root: Path, die_at: int | None = None) -> None:
        super().__init__(root)
        self.ops: list[str] = []
        self.die_at = die_at

    def _op(self, name: str) -> None:
        self.ops.append(name)
        if self.die_at is not None and len(self.ops) >= self.die_at:
            raise Died(name)

    def remove(self, name):
        self._op(name)
        return super().remove(name)

    def remove_all(self, name):
        self._op(name)
        return super().remove_all(name)

    def remove_empty_dir(self, name):
        self._op(name)
        return super().remove_empty_dir(name)


def _three_versions(tmp_path: Path) -> tuple[LocalFS, StorageRoot, Path, str]:
    d = tmp_path / "root"
    d.mkdir()
    fsys = LocalFS(d)
    root = StorageRoot.init(fsys, fsys, layout=HashedNTupleLayout())
    for i in range(3):
        _commit_object(tmp_path, root, fsys, "obj:p", {f"f{i}.txt": str(i)})
    return fsys, root, d, root.object_path("obj:p")


class TestPurgeOrder:
    def test_adr_0001_order(self, tmp_path: Path):
        _fsys, root, d, obj = _three_versions(tmp_path)
        rec = _Recorder(d)
        root.purge(rec, "obj:p")
        ops = [op for op in rec.ops if op.split("/")[0] != "extensions"]
        parents = obj.split("/")
        assert ops == [
            f"{obj}/inventory.json",
            f"{obj}/inventory.json.sha512",
            f"{obj}/v3",
            f"{obj}/v2",
            f"{obj}/v1",
            f"{obj}/0=ocfl_object_1.1",
            obj,
            *("/".join(parents[:i]) for i in range(len(parents) - 1, 0, -1)),
        ]
        assert not (d / parents[0]).exists()
        assert not (d / "extensions" / "NNNN-work-dir").exists()

    def test_every_crash_point_is_object_or_gone(self, tmp_path: Path):
        from ocfl_lib.validation import validate_root

        probe = tmp_path / "probe"
        probe.mkdir()
        fsys, root, d, _obj = _three_versions(probe)
        rec = _Recorder(d)
        root.purge(rec, "obj:p")
        for n in range(1, len(rec.ops) + 1):
            base = tmp_path / f"p{n}"
            base.mkdir()
            fsys, root, d, obj = _three_versions(base)
            with pytest.raises(Died):
                root.purge(_Recorder(d, die_at=n), "obj:p")
            errors = {f.code[:4] for f in validate_root(fsys, "").errors}
            assert "E072" not in errors
            if any(p.is_file() for p in (d / obj).rglob("*")):
                assert root.get_object("obj:p").exists
            try:
                root.purge(fsys, "obj:p")
            except ObjectNotFound:
                assert not (d / obj.split("/")[0]).exists()
            report = validate_root(fsys, "")
            assert report.is_valid
            assert not [f for f in report.findings if f.code.startswith("W016")]

    def test_refuses_stray_claim(self, tmp_path: Path):
        fsys, root, d, obj = _three_versions(tmp_path)
        (d / obj / "v4").mkdir()
        with pytest.raises(RecoveryRequiredError):
            root.purge(fsys, "obj:p")
        assert root.get_object("obj:p").exists

    def test_refuses_live_commit(self, tmp_path: Path):
        from ocfl_lib._commit import work_dir

        fsys, root, _d, _obj = _three_versions(tmp_path)
        with (
            fsys.lock(work_dir("", "obj:p"), timeout=0),
            pytest.raises(ObjectLockedError),
        ):
            root.purge(fsys, "obj:p")
        assert root.get_object("obj:p").exists

    def test_missing_object_leaves_no_work_dir(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        root = StorageRoot.init(fsys, fsys, layout=FlatDirectLayout())
        with pytest.raises(ObjectNotFound):
            root.purge(fsys, "nope")
        assert not (tmp_path / "extensions").exists()
