"""Architecture guards for the lib cleanup (docs/roadmap.md §2)."""

import annotationlib
import ast
import dataclasses
import importlib
import inspect
import pkgutil
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
LIB_SRC = REPO / "packages/ocfl-lib/src/ocfl_lib"
CLI_SRC = REPO / "packages/ocfl-cli/src/ocfl_cli"
INDEX_SRC = REPO / "packages/ocfl-index/src/ocfl_index"
FIXTURES = REPO / "tests/fixtures"


def _py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _tree(p: Path) -> ast.Module:
    return ast.parse(p.read_text(), filename=str(p))


def _lib_module_name(p: Path) -> str:
    rel = p.relative_to(LIB_SRC.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


# --- 1. errors


class TestErrors:
    def _error_classes(self):
        import ocfl_lib

        base = ocfl_lib.OcflError
        seen = []
        stack = [base]
        while stack:
            c = stack.pop()
            if c.__module__.startswith("ocfl_lib"):
                seen.append(c)
            stack.extend(c.__subclasses__())
        return seen

    def test_error_class_enum(self):
        from ocfl_lib import ErrorClass

        assert {m.name for m in ErrorClass} == {
            "USAGE",
            "NOT_FOUND",
            "INVALID",
            "SERVICE",
            "STATE",
            "BUSY",
            "HALT",
            "CONFIG",
            "INTERRUPTED",
        }

    def test_every_error_has_class_and_unique_code(self):
        from ocfl_lib import ErrorClass

        codes: dict[str, type] = {}
        for c in self._error_classes():
            assert "code" in vars(c), f"{c.__name__} lacks its own code"
            assert "error_class" in vars(c), f"{c.__name__} lacks error_class"
            assert isinstance(c.error_class, ErrorClass), c
            assert re.fullmatch(r"[a-z][a-z0-9_]*", c.code), (c, c.code)
            assert c.code not in codes, (c, codes.get(c.code))
            codes[c.code] = c

    def test_every_error_exported(self):
        import ocfl_lib

        for c in self._error_classes():
            assert c.__name__ in ocfl_lib.__all__, c.__name__
            assert getattr(ocfl_lib, c.__name__) is c

    def test_lib_raises_only_ocfl_errors(self):
        """Every `raise X(...)` / `raise X` in lib src (FS backends excepted)
        names an OcflError subclass."""
        import ocfl_lib

        allowed_builtin = {"ExceptionGroup"}
        exempt = {"fs/local.py", "fs/_local.py", "fs/s3.py"}
        bad = []
        for p in _py_files(LIB_SRC):
            rel = p.relative_to(LIB_SRC).as_posix()
            if rel in exempt:
                continue
            for node in ast.walk(_tree(p)):
                if not isinstance(node, ast.Raise) or node.exc is None:
                    continue
                exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
                if not isinstance(exc, ast.Name) or not exc.id[:1].isupper():
                    continue
                if exc.id in allowed_builtin:
                    continue
                cls = getattr(ocfl_lib, exc.id, None)
                if not (isinstance(cls, type) and issubclass(cls, ocfl_lib.OcflError)):
                    bad.append(f"{rel}:{node.lineno} raise {exc.id}")
        assert not bad, "\n".join(bad)

    def test_no_unused_error_classes(self):
        """Every error class is raised in lib src or is a base of one that is."""
        raised: set[str] = set()
        for p in _py_files(LIB_SRC):
            for node in ast.walk(_tree(p)):
                if isinstance(node, ast.Raise) and node.exc is not None:
                    exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
                    if isinstance(exc, ast.Name):
                        raised.add(exc.id)
        classes = self._error_classes()
        used = set()
        for c in classes:
            if c.__name__ in raised:
                used.update(b.__name__ for b in c.__mro__)
        unused = [c.__name__ for c in classes if c.__name__ not in used]
        assert not unused, unused

    def test_compat_bases(self):
        import ocfl_lib

        assert issubclass(ocfl_lib.ObjectNotFound, LookupError)
        assert issubclass(ocfl_lib.VersionNotFound, LookupError)


# --- 3. Object reads; StorageRoot.open


class _BoomFS:
    def __getattr__(self, name):
        raise AssertionError(f"I/O in constructor: {name}")


class TestReadOnlyObjectAndOpen:
    def test_object_has_no_write_methods(self):
        from ocfl_lib import Object

        for name in ("update", "new_update_plan", "apply_update_plan"):
            assert not hasattr(Object, name), name
        for name, fn in inspect.getmembers(Object, inspect.isfunction):
            sig = inspect.signature(fn, annotation_format=annotationlib.Format.STRING)
            assert "WriteFS" not in str(sig), name

    def test_constructors_do_no_io(self):
        from ocfl_lib import Object, StorageRoot

        StorageRoot(_BoomFS())  # ty: ignore[invalid-argument-type]
        Object(_BoomFS(), "x")  # ty: ignore[invalid-argument-type]

    def test_open(self, tmp_path):
        from ocfl_lib import LocalFS, NotAnOcflRoot, Spec, StorageRoot

        with pytest.raises(NotAnOcflRoot):
            StorageRoot.open(LocalFS(tmp_path))
        fs = LocalFS(tmp_path)
        StorageRoot.init(fs, fs, spec=Spec.V1_0)
        root = StorageRoot.open(fs)
        assert root.spec is Spec.V1_0
        assert root.layout is None


# --- 4. Stage -> VersionState


class TestVersionState:
    def test_names(self):
        import ocfl_lib

        assert hasattr(ocfl_lib, "VersionState")
        assert not hasattr(ocfl_lib, "Stage")

    def test_no_stage_identifier_anywhere(self):
        bad = []
        for root in (LIB_SRC, CLI_SRC, INDEX_SRC):
            for p in _py_files(root):
                for node in ast.walk(_tree(p)):
                    name = None
                    if isinstance(node, ast.Name):
                        name = node.id
                    elif isinstance(node, ast.ClassDef):
                        name = node.name
                    elif isinstance(node, ast.alias):
                        name = node.name.split(".")[-1]
                    elif isinstance(node, ast.Attribute):
                        name = node.attr
                    if name == "Stage":
                        bad.append(f"{p}:{getattr(node, 'lineno', '?')}")
        assert not bad, bad

    def test_sources_module(self):
        import ocfl_lib

        cs, fx = ocfl_lib.ContentSource, ocfl_lib.FixitySource
        assert cs.__module__ == fx.__module__
        assert cs.__module__ != ocfl_lib.VersionState.__module__
        assert cs.__module__ != ocfl_lib.UpdatePlan.__module__


# --- 7. layout registry


class TestLayoutRegistry:
    def test_default_frozen(self):
        from ocfl_lib import DEFAULT_LAYOUTS, LayoutRegistry

        assert not hasattr(LayoutRegistry, "register")
        with pytest.raises(
            (AttributeError, TypeError, dataclasses.FrozenInstanceError)
        ):
            DEFAULT_LAYOUTS.foo = 1  # type: ignore[attr-defined]

    def test_custom_registry_passed_explicitly(self, tmp_path):
        from ocfl_lib import (
            DEFAULT_LAYOUTS,
            FlatDirectLayout,
            LocalFS,
            OcflError,
            StorageRoot,
        )

        name = "9999-custom-layout"

        class Custom(FlatDirectLayout):
            @property
            def name(self):
                return name

            @property
            def config(self):
                return None

        custom = DEFAULT_LAYOUTS.with_layout(name, lambda cfg: Custom())
        fs = LocalFS(tmp_path)
        StorageRoot.init(fs, fs, layout=Custom())
        with pytest.raises(OcflError):
            StorageRoot.open(fs)
        root = StorageRoot.open(fs, layouts=custom)
        assert root.layout is not None and root.layout.name == name
        assert name not in DEFAULT_LAYOUTS


# --- 8. duplicates


class TestNoDuplicates:
    def _lib_trees(self):
        return [(p, _tree(p)) for p in _py_files(LIB_SRC)]

    def test_one_join_helper(self):
        defs = [
            f"{p.name}:{n.name}"
            for p, t in self._lib_trees()
            for n in ast.walk(t)
            if isinstance(n, ast.FunctionDef)
            and n.name.lstrip("_") in ("join", "join_path")
        ]
        assert len(defs) == 1, defs

    def test_one_version_regex(self):
        hits = [
            f"{p.name}:{n.lineno}"
            for p, t in self._lib_trees()
            for n in ast.walk(t)
            if isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and n.value.startswith("^v")
            and "\\d" in n.value
        ]
        assert len(hits) == 1, hits

    def test_no_supported_version_sets(self):
        bad = []
        for p, t in self._lib_trees():
            for n in ast.walk(t):
                if isinstance(n, ast.Name) and "SUPPORTED" in n.id:
                    bad.append(f"{p.name}:{n.lineno} {n.id}")
                if isinstance(n, (ast.Set, ast.List, ast.Tuple)) and any(
                    isinstance(e, ast.Constant) and e.value in ("1.0", "1.1")
                    for e in n.elts
                ):
                    bad.append(f"{p.name}:{n.lineno} literal version set")
        assert not bad, bad

    def test_root_uses_layout_config_helpers(self):
        root_py = next(
            p for p in _py_files(LIB_SRC) if p.name in ("root.py", "_root.py")
        )
        text = root_py.read_text()
        assert "ocfl_layout.json" not in text
        assert "config.json" not in text
        assert "msgspec" not in text

    def test_one_multi_digest_loop(self):
        hits = 0
        for p in _py_files(LIB_SRC):
            hits += p.read_text().count(".new_hasher() for ")
        assert hits == 1, hits

    def test_no_fs_utils_exists(self):
        fs = importlib.import_module("ocfl_lib.fs")
        for m in pkgutil.iter_modules(fs.__path__, "ocfl_lib.fs."):
            if "util" in m.name:
                mod = importlib.import_module(m.name)
                assert not hasattr(mod, "exists"), m.name
        assert not hasattr(fs, "exists")
