import hashlib
import json
from typing import TYPE_CHECKING

import pytest
from builders import inventory, version_block

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

from ocfl_lib._errors import (
    LayoutNotConfigured,
    MultipleRootDeclarations,
    NotAnOcflRoot,
)
from ocfl_lib._object import Object
from ocfl_lib._root import StorageRoot
from ocfl_lib._spec import Spec
from ocfl_lib.fs._local import LocalFS
from ocfl_lib.fs._protocols import FileRef


def _make_object(
    parent: Path,
    name: str,
    object_id: str,
    *,
    spec: str = "1.1",
    content: dict[str, bytes] | None = None,
) -> None:
    obj = parent / name
    obj.mkdir(parents=True, exist_ok=True)
    (obj / f"0=ocfl_object_{spec}").write_text(f"ocfl_object_{spec}\n")

    content = content or {}
    manifest: dict[str, list[str]] = {}
    state: dict[str, list[str]] = {}
    content_dir = "content"

    for logical_path, data in content.items():
        digest = hashlib.sha512(data).hexdigest()
        content_path = f"v1/{content_dir}/{logical_path}"
        manifest[digest] = [content_path]
        state[digest] = [logical_path]

    inv = inventory(
        id=object_id,
        type=f"https://ocfl.io/{spec}/spec/#inventory",
        manifest=manifest,
        versions={"v1": version_block(state=state)},
    )

    inv_bytes = json.dumps(inv, sort_keys=True, separators=(",", ":")).encode()
    (obj / "inventory.json").write_bytes(inv_bytes)
    digest = hashlib.sha512(inv_bytes).hexdigest()
    (obj / "inventory.json.sha512").write_text(f"{digest} inventory.json\n")

    v1 = obj / "v1"
    v1.mkdir()
    (v1 / "inventory.json").write_bytes(inv_bytes)
    (v1 / "inventory.json.sha512").write_text(f"{digest} inventory.json\n")

    for logical_path, data in content.items():
        content_file = v1 / content_dir / logical_path
        content_file.parent.mkdir(parents=True, exist_ok=True)
        content_file.write_bytes(data)


def _make_root(
    tmp_path: Path,
    *,
    spec: str = "1.1",
    layout_ext: str | None = None,
    layout_desc: str = "",
) -> Path:
    (tmp_path / f"0=ocfl_{spec}").write_text(f"ocfl_{spec}\n")
    if layout_ext:
        layout_data = {"extension": layout_ext, "description": layout_desc}
        (tmp_path / "ocfl_layout.json").write_text(
            json.dumps(layout_data, separators=(",", ":"))
        )
    return tmp_path


class TestStorageRootOpen:
    def test_spec_from_namaste(self, tmp_path: Path):
        _make_root(tmp_path, spec="1.1")
        fsys = LocalFS(tmp_path)
        root = StorageRoot.open(fsys)
        assert root.spec == Spec.V1_1

    def test_spec_1_0(self, tmp_path: Path):
        _make_root(tmp_path, spec="1.0")
        fsys = LocalFS(tmp_path)
        root = StorageRoot.open(fsys)
        assert root.spec == Spec.V1_0

    def test_no_namaste_raises(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        with pytest.raises(NotAnOcflRoot, match="no OCFL root declaration"):
            StorageRoot.open(fsys)

    def test_multiple_namaste_raises(self, tmp_path: Path):
        (tmp_path / "0=ocfl_1.0").write_text("ocfl_1.0\n")
        (tmp_path / "0=ocfl_1.1").write_text("ocfl_1.1\n")
        fsys = LocalFS(tmp_path)
        with pytest.raises(MultipleRootDeclarations, match="multiple"):
            StorageRoot.open(fsys)


class TestStorageRootFs:
    def test_fs_exposes_underlying_filewalker(self, tmp_path: Path):
        _make_root(tmp_path)
        fsys = LocalFS(tmp_path)
        root = StorageRoot.open(fsys)
        assert root.fs is fsys


class TestStorageRootLayout:
    def test_no_layout(self, tmp_path: Path):
        _make_root(tmp_path)
        fsys = LocalFS(tmp_path)
        root = StorageRoot.open(fsys)
        assert root.layout is None

    def test_flat_direct_layout(self, tmp_path: Path):
        _make_root(tmp_path, layout_ext="0002-flat-direct-storage-layout")
        ext_dir = tmp_path / "extensions" / "0002-flat-direct-storage-layout"
        ext_dir.mkdir(parents=True)
        (ext_dir / "config.json").write_text("{}")
        fsys = LocalFS(tmp_path)
        root = StorageRoot.open(fsys)
        assert root.layout is not None
        assert root.layout.name == "0002-flat-direct-storage-layout"

    def test_unknown_layout_raises(self, tmp_path: Path):
        _make_root(tmp_path, layout_ext="9999-nonexistent")
        fsys = LocalFS(tmp_path)
        with pytest.raises(KeyError, match="unknown layout"):
            StorageRoot.open(fsys)


class TestStorageRootObjectPath:
    def test_object_path_with_layout(self, tmp_path: Path):
        _make_root(tmp_path, layout_ext="0002-flat-direct-storage-layout")
        ext_dir = tmp_path / "extensions" / "0002-flat-direct-storage-layout"
        ext_dir.mkdir(parents=True)
        (ext_dir / "config.json").write_text("{}")
        fsys = LocalFS(tmp_path)
        root = StorageRoot.open(fsys)
        assert root.object_path("my-object") == "my-object"

    def test_object_path_no_layout_raises(self, tmp_path: Path):
        _make_root(tmp_path)
        fsys = LocalFS(tmp_path)
        root = StorageRoot.open(fsys)
        with pytest.raises(LayoutNotConfigured, match="no layout"):
            root.object_path("some-id")


class TestStorageRootGetObject:
    def test_get_object(self, tmp_path: Path):
        _make_root(tmp_path, layout_ext="0002-flat-direct-storage-layout")
        ext_dir = tmp_path / "extensions" / "0002-flat-direct-storage-layout"
        ext_dir.mkdir(parents=True)
        (ext_dir / "config.json").write_text("{}")
        _make_object(tmp_path, "my-obj", "my-obj")
        fsys = LocalFS(tmp_path)
        root = StorageRoot.open(fsys)
        obj = root.get_object("my-obj")
        assert isinstance(obj, Object)
        assert obj.id == "my-obj"

    def test_get_object_no_layout_raises(self, tmp_path: Path):
        _make_root(tmp_path)
        fsys = LocalFS(tmp_path)
        root = StorageRoot.open(fsys)
        with pytest.raises(LayoutNotConfigured, match="no layout"):
            root.get_object("some-id")


class TestStorageRootObjectPaths:
    def test_object_paths_empty_root(self, tmp_path: Path):
        _make_root(tmp_path)
        fsys = LocalFS(tmp_path)
        root = StorageRoot.open(fsys)
        assert list(root.object_paths()) == []

    def test_object_paths_finds_objects(self, tmp_path: Path):
        _make_root(tmp_path)
        _make_object(tmp_path, "obj-a", "http://example.org/a")
        _make_object(tmp_path, "obj-b", "http://example.org/b")
        fsys = LocalFS(tmp_path)
        root = StorageRoot.open(fsys)
        paths = sorted(root.object_paths())
        assert paths == ["obj-a", "obj-b"]

    def test_object_paths_nested(self, tmp_path: Path):
        _make_root(tmp_path)
        _make_object(tmp_path, "abc/def/obj1", "http://example.org/1")
        fsys = LocalFS(tmp_path)
        root = StorageRoot.open(fsys)
        paths = list(root.object_paths())
        assert paths == ["abc/def/obj1"]

    def test_object_paths_swallows_oserror_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _make_root(tmp_path)
        _make_object(tmp_path, "obj-a", "http://example.org/a")
        fsys = LocalFS(tmp_path)
        root = StorageRoot.open(fsys)

        def torn_walk(_name: str) -> Iterator[FileRef]:
            yield FileRef(path="obj-a/0=ocfl_object_1.1", size=0)
            raise OSError("injected")

        monkeypatch.setattr(root._fsys, "walk_files", torn_walk)

        assert list(root.object_paths()) == ["obj-a"]

    def test_object_paths_strict_propagates_oserror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _make_root(tmp_path)
        _make_object(tmp_path, "obj-a", "http://example.org/a")
        fsys = LocalFS(tmp_path)
        root = StorageRoot.open(fsys)

        def torn_walk(_name: str) -> Iterator[FileRef]:
            yield FileRef(path="obj-a/0=ocfl_object_1.1", size=0)
            raise OSError("injected")

        monkeypatch.setattr(root._fsys, "walk_files", torn_walk)

        with pytest.raises(OSError, match="injected"):
            list(root.object_paths(strict=True))


class TestStorageRootObjects:
    def test_objects_iterates(self, tmp_path: Path):
        _make_root(tmp_path)
        _make_object(tmp_path, "obj-a", "http://example.org/a")
        _make_object(tmp_path, "obj-b", "http://example.org/b")
        fsys = LocalFS(tmp_path)
        root = StorageRoot.open(fsys)
        objs = sorted(root.objects(), key=lambda o: o.id)
        assert len(objs) == 2
        assert objs[0].id == "http://example.org/a"
        assert objs[1].id == "http://example.org/b"

    def test_objects_empty_root(self, tmp_path: Path):
        _make_root(tmp_path)
        fsys = LocalFS(tmp_path)
        root = StorageRoot.open(fsys)
        assert list(root.objects()) == []


class TestStorageRootObjectsBatch:
    def test_objects_batch_matches_serial(self, tmp_path: Path):
        _make_root(tmp_path)
        _make_object(tmp_path, "obj-a", "http://example.org/a")
        _make_object(tmp_path, "obj-b", "http://example.org/b")
        _make_object(tmp_path, "obj-c", "http://example.org/c")
        fsys = LocalFS(tmp_path)
        root = StorageRoot.open(fsys)
        serial_ids = sorted(o.id for o in root.objects())
        batch_ids = sorted(o.id for o in root.objects_batch())
        assert serial_ids == batch_ids

    def test_objects_batch_empty_root(self, tmp_path: Path):
        _make_root(tmp_path)
        fsys = LocalFS(tmp_path)
        root = StorageRoot.open(fsys)
        assert list(root.objects_batch()) == []

    def test_objects_batch_with_max_workers(self, tmp_path: Path):
        _make_root(tmp_path)
        _make_object(tmp_path, "obj-a", "http://example.org/a")
        _make_object(tmp_path, "obj-b", "http://example.org/b")
        fsys = LocalFS(tmp_path)
        root = StorageRoot.open(fsys)
        objs = sorted(root.objects_batch(max_workers=2), key=lambda o: o.id)
        assert len(objs) == 2
        assert objs[0].id == "http://example.org/a"
        assert objs[1].id == "http://example.org/b"

    def test_objects_batch_loads_inventories(self, tmp_path: Path):
        _make_root(tmp_path)
        _make_object(
            tmp_path,
            "obj-x",
            "http://example.org/x",
            content={"hello.txt": b"hello"},
        )
        fsys = LocalFS(tmp_path)
        root = StorageRoot.open(fsys)
        objs = list(root.objects_batch())
        assert len(objs) == 1
        assert objs[0].id == "http://example.org/x"
        assert objs[0].head.number == 1
