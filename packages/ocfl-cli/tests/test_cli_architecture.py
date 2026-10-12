"""Architecture guards for the CLI layers (docs/roadmap.md §5, ADR-0002).

The lib's guards live in packages/ocfl-lib/tests/test_architecture.py; these
cover ocfl-cli only.
"""

import ast
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CLI_SRC = REPO / "packages/ocfl-cli/src/ocfl_cli"
CLI_TESTS = REPO / "packages/ocfl-cli/tests"
VENV_BIN = Path(sys.executable).parent


def _py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _tree(p: Path) -> ast.Module:
    return ast.parse(p.read_text(), filename=str(p))


def _rel(p: Path) -> str:
    return p.relative_to(CLI_SRC).as_posix()


# --- 5.4 one error handler: ErrorClass -> ExitCode, JSON errors on stderr


def _mapping(tree: ast.Module) -> ast.FunctionDef:
    return next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "exit_code_for_class"
    )


def _ty(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(VENV_BIN / "ty"), "check", "--python", sys.executable, str(path)],
        cwd=path.parent,
        capture_output=True,
        text=True,
    )


class TestOneErrorHandler:
    def test_only_the_handler_renders_errors_or_maps_exit_codes(self):
        bad = []
        for p in _py_files(CLI_SRC):
            if _rel(p) == "_exit.py":
                continue
            for node in ast.walk(_tree(p)):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and node.value.startswith("Error: ")
                ):
                    bad.append(f"{_rel(p)}:{node.lineno} renders 'Error: '")
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                    if name in {"exit_code_for_class", "exit_for_error", "err"}:
                        bad.append(f"{_rel(p)}:{node.lineno} {name}()")
                    if (
                        name == "Exit"
                        and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and node.args[0].value in {2, 3, 4, 5, 6}
                    ):
                        bad.append(
                            f"{_rel(p)}:{node.lineno} Exit({node.args[0].value})"
                        )
        assert not bad, "\n".join(bad)

    def test_the_root_group_routes_errors_through_the_handler(self):
        from ocfl_cli._exit import OcflGroup
        from ocfl_cli.main import app

        assert app.info.cls is OcflGroup

    def test_mapping_is_one_exhaustive_match(self):
        fn = _mapping(_tree(CLI_SRC / "exit_codes.py"))
        (match,) = [n for n in ast.walk(fn) if isinstance(n, ast.Match)]
        last = match.cases[-1]
        assert isinstance(last.pattern, ast.MatchAs) and last.pattern.pattern is None
        assert "assert_never" in ast.unparse(last.body[0])
        for case in match.cases[:-1]:
            assert "ErrorClass." in ast.unparse(case.pattern)

    def test_ty_rejects_a_missing_case(self, tmp_path):
        src = (CLI_SRC / "exit_codes.py").read_text()
        tree = ast.parse(src)
        match = next(n for n in ast.walk(_mapping(tree)) if isinstance(n, ast.Match))
        match.cases = [c for c in match.cases if "HALT" not in ast.unparse(c.pattern)]
        bad = tmp_path / "exit_codes_missing_case.py"
        bad.write_text(ast.unparse(tree))
        out = _ty(bad)
        assert out.returncode != 0, out.stdout
        assert "assert" in out.stdout.lower() or "never" in out.stdout.lower()

    def test_cli_errors_carry_a_class_and_a_unique_code(self):
        from ocfl_cli.errors import CLIError
        from ocfl_lib import ErrorClass, OcflError

        lib_codes = set()
        lib_stack: list[type[OcflError]] = [OcflError]
        while lib_stack:
            lc = lib_stack.pop()
            lib_codes.add(lc.code)
            lib_stack.extend(lc.__subclasses__())
        seen: dict[str, type] = {}
        stack: list[type[CLIError]] = [CLIError]
        while stack:
            c = stack.pop()
            stack.extend(c.__subclasses__())
            assert "code" in vars(c) and "error_class" in vars(c), c
            assert isinstance(c.error_class, ErrorClass), c
            assert re.fullmatch(r"[a-z][a-z0-9_]*", c.code), c
            assert c.code not in seen and c.code not in lib_codes, c
            seen[c.code] = c

    def test_bitcoin_errors_are_the_libs(self):
        """The phase-4 stopgap that re-raised lib Bitcoin errors as CLI ones is gone."""
        import ocfl_cli.errors as cli_errors

        assert not [n for n in vars(cli_errors) if n.startswith("Bitcoin")]
        text = (CLI_SRC / "ots/bitcoin.py").read_text()
        assert "_cli_errors" not in text and "class BitcoinRPCClient" not in text
        assert "return BitcoinRpcClient(" in text
