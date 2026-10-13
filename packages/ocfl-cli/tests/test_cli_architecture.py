"""Architecture guards for the CLI layers (docs/roadmap.md §5, ADR-0002).

The lib's guards live in packages/ocfl-lib/tests/test_architecture.py; these
cover ocfl-cli only.
"""

import ast
import importlib.util
import re
import subprocess
import sys
import tomllib
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


def _module(p: Path) -> str:
    parts = list(p.relative_to(CLI_SRC.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError, ValueError:
        return False


def _contracts() -> list[dict]:
    proj = tomllib.loads((REPO / "pyproject.toml").read_text())
    return proj["tool"]["importlinter"]["contracts"]


# --- 5.1 layers: commands -> use cases -> adapters, enforced by import-linter

COMMANDS = ("archive", "stage", "_appctx", "_common", "_exit", "_style", "exit_codes")
ADAPTERS = ("ots", "store")
BELOW_COMMANDS = ("workflow", *ADAPTERS, "model", "ports", "config", "errors")


class TestLayers:
    def test_import_linter_contracts_hold(self):
        out = subprocess.run(
            [str(VENV_BIN / "lint-imports")],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        assert out.returncode == 0, out.stdout + out.stderr

    def test_the_contracts_state_the_layers(self):
        by_type = {c["type"]: c for c in _contracts() if c["type"] == "layers"}
        layers = by_type["layers"]
        assert layers["containers"] == ["ocfl_cli"] and layers["exhaustive"]
        order = layers["layers"]
        assert order[0] == "main"
        assert order.index("archive | stage") < order.index("workflow")
        assert order.index("workflow") < order.index("ots | store")
        assert order.index("ots | store") < order.index("system")
        forbidden = [c for c in _contracts() if c["type"] == "forbidden"]
        assert any(c["source_modules"] == ["ocfl_lib"] for c in forbidden)
        commands = next(c for c in forbidden if "ocfl_cli.stage" in c["source_modules"])
        assert set(commands["forbidden_modules"]) == {f"ocfl_cli.{a}" for a in ADAPTERS}

    def test_no_private_names_cross_modules(self):
        """A `_name` is private to its module, a `_module` to its package."""
        bad: list[str] = []

        def check_module(full: str, me: str, where: str) -> None:
            parts = full.split(".")
            for i, seg in enumerate(parts):
                if seg.startswith("_") and not seg.startswith("__"):
                    parent = ".".join(parts[:i])
                    if not (me == parent or me.startswith(parent + ".")):
                        bad.append(f"{where} {full}")
                    return

        for p in _py_files(CLI_SRC):
            me = _module(p)
            tree = _tree(p)
            aliases: dict[str, str] = {}
            for node in ast.walk(tree):
                where = f"{_rel(p)}:{getattr(node, 'lineno', '?')}"
                if isinstance(node, ast.Import):
                    for a in node.names:
                        if a.name.split(".")[0] == "ocfl_cli":
                            check_module(a.name, me, where)
                            if a.asname:
                                aliases[a.asname] = a.name
                elif (
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    and node.module.split(".")[0] == "ocfl_cli"
                ):
                    check_module(node.module, me, where)
                    for a in node.names:
                        full = f"{node.module}.{a.name}"
                        if _is_module(full):
                            check_module(full, me, where)
                            aliases[a.asname or a.name] = full
                        elif a.name.startswith("_") and not a.name.startswith("__"):
                            bad.append(f"{where} {full}")
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in aliases
                    and node.attr.startswith("_")
                    and not node.attr.startswith("__")
                ):
                    bad.append(
                        f"{_rel(p)}:{node.lineno} {aliases[node.value.id]}.{node.attr}"
                    )
        assert not bad, "\n".join(bad)

    def test_below_the_commands_there_is_no_ui(self):
        ui = {"typer", "click", "rich"}
        bad = []
        for pkg in BELOW_COMMANDS:
            path = CLI_SRC / pkg
            for p in _py_files(path) if path.is_dir() else [path.with_suffix(".py")]:
                for node in ast.walk(_tree(p)):
                    if isinstance(node, ast.ImportFrom) and node.module:
                        mods = [node.module]
                    elif isinstance(node, ast.Import):
                        mods = [a.name for a in node.names]
                    else:
                        continue
                    bad += [f"{_rel(p)} {m}" for m in mods if m.split(".")[0] in ui]
        assert not bad, bad

    def test_service_and_watch_moved_to_the_use_cases(self):
        assert not (CLI_SRC / "stage/service.py").exists()
        assert not (CLI_SRC / "stage/watch.py").exists()
        assert not (CLI_SRC / "stage/commands.py").exists()
        for mod in ("staging", "status", "content", "inventory", "commit", "watch"):
            assert (CLI_SRC / "workflow" / f"{mod}.py").is_file(), mod


# --- 5.2 the batch engine lives in the use cases


class TestBatchEngine:
    def test_engine_is_a_use_case_package(self):
        engine = {p.stem for p in _py_files(CLI_SRC / "workflow/batch")}
        assert {"drive", "prepare", "submit", "retry", "upgrade", "watch"} <= engine

    def test_commands_hold_no_engine(self):
        """Batch commands parse options and render events; nothing else."""
        bad = []
        for p in _py_files(CLI_SRC / "stage"):
            text = p.read_text()
            for needle in (
                "save_batch(",
                "load_batch(",
                "with_batch_lock(",
                "with_driver_lease(",
                "with_lock(",
                "submit_hashes(",
                "verify_batch(",
            ):
                if needle in text:
                    bad.append(f"{_rel(p)} {needle}")
        assert not bad, bad

    def test_no_contract_lists_exemptions(self):
        assert not [c["name"] for c in _contracts() if c.get("ignore_imports")]

    def test_no_module_over_1000_lines(self):
        big = {
            _rel(p): n
            for p in _py_files(CLI_SRC)
            if (n := len(p.read_text().splitlines())) > 1000
        }
        assert not big, big


# --- 5.3 ports for Clock, Sleeper and BlockStream; fakes, not patched internals


class TestPorts:
    def test_time_ports_are_protocols(self):
        from fakes import FakeClock, InMemoryHashBlockStream, RecordingSleeper

        from ocfl_cli import ports

        assert isinstance(FakeClock(), ports.Clock)
        assert isinstance(RecordingSleeper(), ports.Sleeper)
        assert isinstance(InMemoryHashBlockStream(), ports.BlockStream)

    def test_appctx_carries_the_ports(self):
        import dataclasses

        from ocfl_cli._appctx import AppCtx

        names = {f.name for f in dataclasses.fields(AppCtx)}
        ports = {
            "clock",
            "sleeper",
            "block_stream_factory",
            "lock_timeout",
            "fs_factory",
        }
        assert ports <= names

    def test_only_the_system_adapter_reads_time(self):
        homes = set()
        for p in _py_files(CLI_SRC):
            for node in ast.walk(_tree(p)):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and (node.value.id, node.attr)
                    in {
                        ("time", "sleep"),
                        ("time", "time"),
                        ("time", "monotonic"),
                        ("datetime", "now"),
                        ("datetime", "utcnow"),
                    }
                ):
                    homes.add(_rel(p))
        assert homes == {"system.py"}, homes

    def test_tests_patch_no_cli_module(self):
        """Tests inject fakes through `ctx.obj` / `Runtime`, not module internals.

        Covers `monkeypatch` and `unittest.mock.patch` / `patch.object`.
        """
        bad = []
        for p in _py_files(CLI_TESTS):
            tree = _tree(p)
            modules: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.split(".")[0] != "ocfl_cli":
                        continue
                    for a in node.names:
                        if _is_module(f"{node.module}.{a.name}"):
                            modules.add(a.asname or a.name)
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        if a.name.split(".")[0] == "ocfl_cli":
                            modules.add(a.asname or a.name.split(".")[0])
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and node.args):
                    continue
                func = ast.unparse(node.func)
                monkeypatched = func in {"monkeypatch.setattr", "monkeypatch.delattr"}
                mocked = func in {"patch", "mock.patch", "patch.object"}
                if not (monkeypatched or mocked):
                    continue
                target = node.args[0]
                if isinstance(target, ast.Constant):
                    if str(target.value).startswith("ocfl_cli"):
                        bad.append(f"{p.name}:{node.lineno}")
                    continue
                while isinstance(target, ast.Attribute):
                    target = target.value
                if isinstance(target, ast.Name) and target.id in modules:
                    bad.append(f"{p.name}:{node.lineno}")
        assert not bad, bad


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


# --- 5.5 consoles per command run, in AppCtx, not _style globals


class TestConsolesPerRun:
    def test_style_holds_no_console(self):
        tree = _tree(CLI_SRC / "_style.py")
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.Global)]
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                assert "Console(" not in ast.unparse(node), ast.unparse(node)

    def test_only_appctx_and_the_handler_make_consoles(self):
        homes = {
            _rel(p)
            for p in _py_files(CLI_SRC)
            if "make_console(" in p.read_text() or "Console(" in p.read_text()
        }
        assert homes <= {"_style.py", "_appctx.py", "_exit.py"}, homes

    def test_no_color_does_not_leak_into_the_next_run(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner

        from ocfl_cli.main import app
        from ocfl_lib import LocalFS, Spec, StorageRoot, get_layout

        fs = LocalFS(tmp_path)
        layout = get_layout("0002-flat-direct-storage-layout")
        StorageRoot.init(fs, fs, spec=Spec.V1_1, layout=layout)
        monkeypatch.delenv("NO_COLOR", raising=False)
        runner = CliRunner()
        env = {"FORCE_COLOR": "1"}
        args = ["-r", str(tmp_path), "info"]
        plain = runner.invoke(app, ["--no-color", *args], env=env)
        coloured = runner.invoke(app, args, env=env)
        assert plain.exit_code == coloured.exit_code == 0
        assert "\x1b[" not in plain.stdout
        assert "\x1b[" in coloured.stdout
