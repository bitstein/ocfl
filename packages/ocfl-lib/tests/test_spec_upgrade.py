"""Spec version rules on write: E081 (root), E103 (no downgrade), E003/E038."""

from typing import TYPE_CHECKING

import pytest

from ocfl_lib._digest import DigestAlgorithm
from ocfl_lib._errors import SpecVersionError
from ocfl_lib._layout import FlatDirectLayout
from ocfl_lib._root import StorageRoot
from ocfl_lib._spec import Spec
from ocfl_lib._version_state import VersionState, scan_dir
from ocfl_lib.fs._local import LocalFS
from ocfl_lib.validation._object import validate_object
from ocfl_lib.validation._root import validate_root

if TYPE_CHECKING:
    from pathlib import Path

_CREATED = "2024-01-01T00:00:00Z"


def _stage(tmp_path: Path, content: str) -> VersionState:
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "file.txt").write_text(content)
    return scan_dir(LocalFS(tmp_path), "src", DigestAlgorithm.SHA512)


def _root(tmp_path: Path, spec: Spec) -> tuple[LocalFS, StorageRoot]:
    store = tmp_path / "root"
    store.mkdir()
    fsys = LocalFS(store)
    return fsys, StorageRoot.init(fsys, fsys, layout=FlatDirectLayout(), spec=spec)


def _object_namastes(obj_dir: Path) -> list[str]:
    return sorted(p.name for p in obj_dir.iterdir() if p.name.startswith("0="))


def _assert_valid_object(fsys: LocalFS, path: str) -> None:
    r = validate_object(fsys, path)
    assert r.is_valid, [e.code for e in r.errors]


class TestE081RootRefusal:
    def test_new_1_1_object_in_1_0_root_refused(self, tmp_path: Path):
        fsys, root = _root(tmp_path, Spec.V1_0)

        with pytest.raises(SpecVersionError, match="E081"):
            root.commit(fsys, "obj", _stage(tmp_path, "a"), spec=Spec.V1_1)

        assert not (tmp_path / "root" / "obj").exists()
        r = validate_root(fsys, "")
        assert r.is_valid, [e.code for e in r.errors]

    def test_upgrade_to_1_1_in_1_0_root_refused(self, tmp_path: Path):
        fsys, root = _root(tmp_path, Spec.V1_0)
        root.commit(fsys, "obj", _stage(tmp_path, "a"), created=_CREATED)

        with pytest.raises(SpecVersionError, match="E081"):
            root.commit(fsys, "obj", _stage(tmp_path, "b"), spec=Spec.V1_1)

        obj_dir = tmp_path / "root" / "obj"
        assert _object_namastes(obj_dir) == ["0=ocfl_object_1.0"]
        assert not (obj_dir / "v2").exists()
        _assert_valid_object(fsys, "obj")


class TestE103DowngradeRefusal:
    def test_downgrade_refused(self, tmp_path: Path):
        fsys, root = _root(tmp_path, Spec.V1_1)
        root.commit(fsys, "obj", _stage(tmp_path, "a"), created=_CREATED)

        with pytest.raises(SpecVersionError, match="E103"):
            root.commit(fsys, "obj", _stage(tmp_path, "b"), spec=Spec.V1_0)

        obj_dir = tmp_path / "root" / "obj"
        assert _object_namastes(obj_dir) == ["0=ocfl_object_1.1"]
        assert not (obj_dir / "v2").exists()


class TestNamasteSwap:
    def test_upgrade_swaps_namaste(self, tmp_path: Path):
        fsys, root = _root(tmp_path, Spec.V1_1)
        root.commit(
            fsys, "obj", _stage(tmp_path, "a"), spec=Spec.V1_0, created=_CREATED
        )

        obj = root.commit(
            fsys, "obj", _stage(tmp_path, "b"), spec=Spec.V1_1, created=_CREATED
        )

        assert obj.spec == Spec.V1_1
        obj_dir = tmp_path / "root" / "obj"
        assert _object_namastes(obj_dir) == ["0=ocfl_object_1.1"]
        assert (obj_dir / "0=ocfl_object_1.1").read_text() == "ocfl_object_1.1\n"
        _assert_valid_object(fsys, "obj")

    def test_same_spec_update_keeps_single_namaste(self, tmp_path: Path):
        fsys, root = _root(tmp_path, Spec.V1_0)
        root.commit(fsys, "obj", _stage(tmp_path, "a"), created=_CREATED)
        root.commit(fsys, "obj", _stage(tmp_path, "b"), created=_CREATED)

        assert _object_namastes(tmp_path / "root" / "obj") == ["0=ocfl_object_1.0"]
        _assert_valid_object(fsys, "obj")

    def test_failed_publish_after_swap_restores_old_namaste(self, tmp_path: Path):
        fsys, root = _root(tmp_path, Spec.V1_1)
        root.commit(
            fsys, "obj", _stage(tmp_path, "a"), spec=Spec.V1_0, created=_CREATED
        )

        class FailRootInventory(LocalFS):
            def write(self, name, data):
                if name == "obj/inventory.json":
                    raise RuntimeError("injected")
                return super().write(name, data)

        with pytest.raises(RuntimeError, match="injected"):
            root.commit(
                FailRootInventory(tmp_path / "root"),
                "obj",
                _stage(tmp_path, "b"),
                spec=Spec.V1_1,
                created=_CREATED,
            )

        obj_dir = tmp_path / "root" / "obj"
        assert _object_namastes(obj_dir) == ["0=ocfl_object_1.0"]
        assert not (obj_dir / "v2").exists()
        _assert_valid_object(fsys, "obj")

    def test_swap_runs_just_before_root_inventory(self, tmp_path: Path):
        fsys, root = _root(tmp_path, Spec.V1_1)
        root.commit(
            fsys, "obj", _stage(tmp_path, "a"), spec=Spec.V1_0, created=_CREATED
        )
        ops: list[tuple[str, str]] = []

        class Recorder(LocalFS):
            def write(self, name, data):
                ops.append(("write", name))
                return super().write(name, data)

            def remove(self, name):
                ops.append(("remove", name))
                return super().remove(name)

        root.commit(
            Recorder(tmp_path / "root"),
            "obj",
            _stage(tmp_path, "b"),
            spec=Spec.V1_1,
            created=_CREATED,
        )
        top = [op for op in ops if op[1].count("/") == 1 and op[1].startswith("obj/")]
        assert top[-4:] == [
            ("write", "obj/0=ocfl_object_1.1"),
            ("remove", "obj/0=ocfl_object_1.0"),
            ("write", "obj/inventory.json"),
            ("write", "obj/inventory.json.sha512"),
        ]


class TestE081Validation:
    def _root_with_object(self, tmp_path: Path, obj_spec: Spec) -> LocalFS:
        fsys, root = _root(tmp_path, Spec.V1_1)
        root.commit(fsys, "obj", _stage(tmp_path, "a"), spec=obj_spec)
        return fsys

    def _downgrade_root_namaste(self, tmp_path: Path) -> None:
        store = tmp_path / "root"
        (store / "0=ocfl_1.1").unlink()
        (store / "0=ocfl_1.0").write_text("ocfl_1.0\n")

    def test_1_1_object_in_1_0_root_reports_e081(self, tmp_path: Path):
        fsys = self._root_with_object(tmp_path, Spec.V1_1)
        self._downgrade_root_namaste(tmp_path)

        r = validate_root(fsys, "")

        e081 = [e for e in r.errors if e.code == "E081"]
        assert len(e081) == 1
        assert "obj" in e081[0].render()

    def test_1_0_object_in_1_0_root_ok(self, tmp_path: Path):
        fsys = self._root_with_object(tmp_path, Spec.V1_0)
        self._downgrade_root_namaste(tmp_path)

        r = validate_root(fsys, "")

        assert r.is_valid, [e.code for e in r.errors]

    def test_1_0_object_in_1_1_root_ok(self, tmp_path: Path):
        fsys = self._root_with_object(tmp_path, Spec.V1_0)

        r = validate_root(fsys, "")

        assert r.is_valid, [e.code for e in r.errors]
