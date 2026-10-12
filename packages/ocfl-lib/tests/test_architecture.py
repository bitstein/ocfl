"""Architecture guards for the lib cleanup (docs/roadmap.md §2)."""

import ast
import re
from pathlib import Path

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


# ------------------------------------------------------------ 1. errors


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
