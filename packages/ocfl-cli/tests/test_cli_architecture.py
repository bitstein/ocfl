import ast
import importlib.util
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from ocfl_cli.exit_codes import ExitCode

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
        assert not bad, (
            "a `_name` is private to its module, a `_module` to its package:\n"
            + "\n".join(bad)
        )

    def test_tests_use_no_private_names(self):
        bad: list[str] = []
        for p in _py_files(CLI_TESTS):
            tree = _tree(p)
            aliases: dict[str, str] = {}
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    and node.module.split(".")[0] == "ocfl_cli"
                ):
                    continue
                for a in node.names:
                    full = f"{node.module}.{a.name}"
                    if _is_module(full):
                        aliases[a.asname or a.name] = full
                    elif a.name.startswith("_") and not a.name.startswith("__"):
                        bad.append(f"{p.name}:{node.lineno} {full}")
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in aliases
                    and node.attr.startswith("_")
                    and not node.attr.startswith("__")
                ):
                    full = f"{aliases[node.value.id]}.{node.attr}"
                    bad.append(f"{p.name}:{node.lineno} {full}")
        assert not bad, "tests use only public names or `_module`s:\n" + "\n".join(bad)

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
        for mod in ("staging", "status", "timestamp", "targets", "commit", "watch"):
            assert (CLI_SRC / "workflow" / f"{mod}.py").is_file(), mod


class TestBatchEngine:
    def test_engine_is_a_use_case_package(self):
        engine = {p.stem for p in _py_files(CLI_SRC / "workflow/batch")}
        assert {"drive", "prepare", "submit", "retry", "upgrade", "watch"} <= engine

    def test_commands_hold_no_engine(self):
        bad = []
        for p in _py_files(CLI_SRC / "stage"):
            text = p.read_text()
            for needle in (
                "save_batch(",
                "load_batch(",
                "batch_lock(",
                "flock(",
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
        from ocfl_cli.ports import Ports

        assert {f.name for f in dataclasses.fields(Ports)} == {
            "sp_client_factory",
            "btc_rpc_factory",
            "block_stream_factory",
            "clock",
            "sleeper",
            "fs_factory",
            "lock_timeout",
        }
        names = {f.name for f in dataclasses.fields(AppCtx)}
        assert {
            "runtime",
            "fs_factory",
            "sp_client_factory",
            "btc_rpc_factory",
        } <= names


class TestAppCtx:
    @staticmethod
    def _entry_points():
        from ocfl_cli.archive.config import config_app
        from ocfl_cli.main import app
        from ocfl_cli.stage._app import stage_callback

        commands = (*app.registered_commands, *config_app.registered_commands)
        return [*(c.callback for c in commands), stage_callback]

    def test_every_entry_point_reads_the_run_state_first(self):
        import inspect
        import textwrap

        bad = []
        for fn in self._entry_points():
            node = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
            assert isinstance(node, ast.FunctionDef)
            body = node.body[1:] if ast.get_docstring(node) is not None else node.body
            first = ast.unparse(body[0])
            if not re.fullmatch(
                r"(\w+ = )?appctx\.(of|tolerating_broken_config)\(ctx\)", first
            ):
                bad.append(f"{fn.__module__}.{fn.__name__}: {first}")
        assert not bad, bad

    def test_only_the_config_commands_tolerate_a_broken_config(self):
        homes = {
            _rel(p)
            for p in _py_files(CLI_SRC)
            if "tolerating_broken_config(" in p.read_text()
        }
        assert homes == {"_appctx.py", "archive/config.py"}, homes

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
        import ocfl_cli.errors as cli_errors

        assert not [n for n in vars(cli_errors) if n.startswith("Bitcoin")]
        text = (CLI_SRC / "ots/bitcoin.py").read_text()
        assert "_cli_errors" not in text and "class BitcoinRPCClient" not in text
        assert "return BitcoinRpcClient(" in text


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
        assert plain.exit_code == coloured.exit_code == ExitCode.SUCCESS
        assert "\x1b[" not in plain.stdout
        assert "\x1b[" in coloured.stdout


class TestOneCommitBlock:
    def test_lock_commit_and_purge_happen_once(self):
        text = (CLI_SRC / "workflow/commit.py").read_text()
        for needle in ("object_commit_lock(", "with_lock(", ".purge(", "sr.commit("):
            assert text.count(needle) == 1, needle

    def test_both_paths_delegate_to_the_block(self):
        tree = _tree(CLI_SRC / "workflow/commit.py")
        fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        for name in ("commit_bare", "commit_ots"):
            calls = {
                ast.unparse(n.func)
                for n in ast.walk(fns[name])
                if isinstance(n, ast.Call)
            }
            assert "_commit_staging" in calls, name


class TestProofsByDigest:
    def test_one_proof_table_for_both_targets(self):
        import msgspec

        from ocfl_cli.model.ots_state import OTS_STATE_VERSION, OtsState, Submission
        from ocfl_cli.model.phase import TimestampTarget

        assert OTS_STATE_VERSION == 2
        fields = {f.name for f in msgspec.structs.fields(OtsState)}
        assert "proofs" in fields
        assert not {"content_proofs", "inventory_proof"} & fields
        target = {f.name: f.type for f in msgspec.structs.fields(Submission)}["target"]
        assert target is TimestampTarget

    def test_workflow_classifies_paths_with_split_state(self):
        bad = [
            f"{_rel(p)}:{n.lineno}"
            for p in _py_files(CLI_SRC / "workflow")
            for n in ast.walk(_tree(p))
            if isinstance(n, ast.Call)
            and ast.unparse(n.func).endswith(".endswith")
            and "ots" in ast.unparse(n.args[0]).lower()
        ]
        assert not bad, bad
        assert "ots.split_state(" in (CLI_SRC / "workflow/common.py").read_text()

    def test_moving_back_keeps_proofs_and_the_plan(self):
        tree = _tree(CLI_SRC / "workflow/staging.py")
        fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        for name in ("apply_rm_mv_regression", "apply_metadata_ots_regression"):
            src = ast.unparse(fns[name])
            assert "unlink" not in src, name
            assert "proofs=" not in src and "verify_jobs=" not in src, name


def _compares_a_target(node: ast.AST) -> bool:
    def is_target(n: ast.AST) -> bool:
        if isinstance(n, ast.Constant):
            return n.value in {"content", "inventory"}
        return isinstance(n, ast.Attribute) and n.attr in {"CONTENT", "INVENTORY"}

    if isinstance(node, ast.Compare):
        return any(is_target(n) for n in (node.left, *node.comparators))
    if isinstance(node, ast.match_case):
        return any(is_target(n) for n in ast.walk(node.pattern))
    if isinstance(node, ast.Dict):
        return any(k is not None and is_target(k) for k in node.keys)
    return False


class TestOneTimestampWorkflow:
    def test_no_per_target_use_case_modules(self):
        workflow = CLI_SRC / "workflow"
        assert not (workflow / "content.py").exists()
        assert not (workflow / "inventory.py").exists()

    def test_only_the_target_rules_branch_on_a_target(self):
        bad = [
            f"{_rel(p)}: {ast.unparse(n)[:60]}"
            for p in _py_files(CLI_SRC / "workflow")
            if p.name != "targets.py"
            for n in ast.walk(_tree(p))
            if _compares_a_target(n)
        ]
        assert not bad, bad

    def test_no_content_inventory_twins(self):
        names = {
            n.name
            for p in _py_files(CLI_SRC / "workflow")
            for n in ast.walk(_tree(p))
            if isinstance(n, ast.FunctionDef | ast.ClassDef)
        }
        twins = sorted(
            n
            for n in names
            if (
                {n.replace("content", "inventory"), n.replace("Content", "Inventory")}
                - {n}
            )
            & names
        )
        assert not twins, twins

    def test_one_block_watch_loop(self):
        calls = [
            n
            for n in ast.walk(_tree(CLI_SRC / "workflow/watch.py"))
            if isinstance(n, ast.Call)
            and ast.unparse(n.func).endswith("wait_for_block")
        ]
        assert len(calls) == 1

    def test_every_phase_has_a_target_but_committed(self):
        from ocfl_cli.model.phase import Phase, target_for
        from ocfl_cli.workflow.targets import rules_for

        for phase in Phase:
            target = target_for(phase)
            assert (target is None) == (phase is Phase.COMMITTED), phase
            if target is not None:
                assert rules_for(target).target is target


def _click(app):
    import typer.main

    return typer.main.get_command(app)


class TestCliSurface:
    def test_timestamp_upgrade_retry_are_verbs(self):
        from ocfl_cli.stage import stage_app

        commands = _click(stage_app).commands
        for verb in ("timestamp", "upgrade", "retry", "batch"):
            assert verb in commands, verb
        assert not hasattr(commands["timestamp"], "commands"), "timestamp is a group"
        for name in ("timestamp", "upgrade", "retry"):
            opts = {o for p in commands[name].params for o in p.opts}
            assert "--expect" in opts, name

    def test_batch_verbs(self):
        from ocfl_cli.stage import stage_app

        batch = _click(stage_app).commands["batch"]
        assert set(batch.commands) == {
            "create",
            "submit",
            "upgrade",
            "retry",
            "commit",
            "status",
            "ls",
            "rm",
        }

    def test_no_mode_flag_options_anywhere(self):
        from ocfl_cli.main import app

        bad = []

        def walk(cmd, path: str) -> None:
            for p in cmd.params:
                if {"--upgrade", "--retry", "--batch"} & set(p.opts):
                    bad.append(f"{path} {p.opts}")
            for name, sub in getattr(cmd, "commands", {}).items():
                walk(sub, f"{path} {name}")

        walk(_click(app), "ocfl")
        assert not bad, bad

    def test_hints_come_from_one_module(self):
        bad = [
            f"{_rel(p)}:{n.lineno}"
            for p in _py_files(CLI_SRC)
            if _rel(p) != "workflow/hints.py"
            for n in ast.walk(_tree(p))
            if isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and n.value.lstrip().startswith("ocfl stage ")
        ]
        assert not bad, bad


class TestDiscardAndConfig:
    def test_discard_replaces_reset(self):
        from ocfl_cli.model.phase import Phase, is_legal
        from ocfl_cli.stage import stage_app

        commands = _click(stage_app).commands
        assert "discard" in commands and "reset" not in commands
        assert all(is_legal(p, "discard") for p in Phase if p is not Phase.COMMITTED)
        assert not any(is_legal(p, "reset") for p in Phase)

    def test_config_group(self):
        from ocfl_cli.main import app

        config = _click(app).commands["config"]
        assert set(config.commands) == {"path", "show", "check"}

    def test_show_redacts_every_secret_field(self):
        import json

        from pydantic import SecretStr

        from ocfl_cli.config import BitcoinConfig, Config, SimpleProofConfig, redacted

        secret_fields = {
            name
            for model in (SimpleProofConfig, BitcoinConfig)
            for name, f in model.model_fields.items()
            if f.annotation is SecretStr
        }
        assert secret_fields == {"access_token", "api_key", "rpc_password"}
        cfg = Config(
            simpleproof=SimpleProofConfig(
                base_url="https://sp.example",
                proof_category_id=1,
                access_token=SecretStr("s-1"),
                api_key=SecretStr("s-2"),
            ),
            bitcoin=BitcoinConfig(
                rpc_url="http://127.0.0.1:8332",
                zmq_endpoint="tcp://127.0.0.1:28332",
                rpc_user="s-3",
                rpc_password=SecretStr("s-4"),
            ),
        )
        text = json.dumps(redacted(cfg))
        assert not [s for s in ("s-1", "s-2", "s-3", "s-4") if s in text]


class TestLocks:
    def test_only_store_locks_imports_fcntl_and_nothing_imports_filelock(self):
        homes = set()
        for p in _py_files(CLI_SRC):
            for node in ast.walk(_tree(p)):
                if isinstance(node, ast.Import | ast.ImportFrom):
                    names = (
                        [a.name for a in node.names]
                        if isinstance(node, ast.Import)
                        else [node.module or ""]
                    )
                    assert "filelock" not in names, _rel(p)
                    if "fcntl" in names:
                        homes.add(_rel(p))
        assert homes == {"store/locks.py"}, homes

    def test_the_wait_is_5_seconds_of_the_sleeper(self):
        from ocfl_cli import ports

        assert ports.DEFAULT_LOCK_TIMEOUT_SECONDS == 5.0
        text = (CLI_SRC / "store/locks.py").read_text()
        assert "runtime.sleeper.sleep(" in text and "LOCK_NB" in text

    def test_no_lease_file_or_heartbeat(self):
        from ocfl_cli.model import batch as model_batch
        from ocfl_cli.store import batch as store_batch

        assert not hasattr(model_batch, "DriverLease")
        names = {n for n in vars(store_batch) if "lease" in n.lower()}
        assert not names, names
        text = (CLI_SRC / "store/batch.py").read_text()
        for needle in (".driver", "heartbeat", "signal", "threading"):
            assert needle not in text, needle

    def test_every_batch_driver_holds_the_batch_lock(self):
        tree = _tree(CLI_SRC / "workflow/batch/drive.py")
        fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        for name in ("submit", "retry", "upgrade", "watch", "commit"):
            calls = {
                ast.unparse(n.func)
                for n in ast.walk(fns[name])
                if isinstance(n, ast.Call)
            }
            assert "batch_state.batch_lock" in calls, name

    def test_the_engine_takes_no_other_batch_lock(self):
        bad = [
            f"{_rel(p)}:{n.lineno}"
            for p in _py_files(CLI_SRC / "workflow/batch")
            if p.name not in {"drive.py", "report.py"}
            for n in ast.walk(_tree(p))
            if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("batch_lock")
        ]
        assert not bad, (
            f"driver holds the batch lock; a second flock self-blocks: {bad}"
        )


class TestSurfaceReview:
    def test_every_issue_has_one_decision(self):
        text = (REPO / "docs/cli-surface-review.md").read_text()
        rows = {}
        for line in text.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 3 and cells[0].isdigit():
                rows[int(cells[0])] = " ".join(cells[1:]).lower()
        assert set(rows) == set(range(1, 38))
        for n, row in rows.items():
            assert ("adopt" in row) != ("reject" in row), n

    def test_exit_codes_stay_the_public_contract(self):
        from ocfl_cli.exit_codes import EXIT_CODE_DOCS, ExitCode

        assert {int(c) for c in ExitCode} == {0, 1, 2, 3, 4, 5, 6, 75, 130}
        (halt,) = [d for d in EXIT_CODE_DOCS if d.code is ExitCode.ATTESTATION]
        assert "retry" not in halt.description

    def test_only_the_root_has_a_version_flag(self):
        from ocfl_cli.main import app

        root = _click(app)
        assert any("--version" in p.opts for p in root.params)
        bad = []

        def walk(cmd, path: str) -> None:
            for p in cmd.params:
                if "--version" in p.opts:
                    bad.append(path)
            for name, sub in getattr(cmd, "commands", {}).items():
                walk(sub, f"{path} {name}")

        for name, sub in root.commands.items():
            walk(sub, name)
        assert not bad, bad

    def test_no_short_only_or_clashing_flags(self):
        from ocfl_cli.main import app

        bad = []

        def walk(cmd, path: str, top: bool) -> None:
            for p in cmd.params:
                opts = getattr(p, "opts", [])
                if p.param_type_name != "option":
                    continue
                if not any(o.startswith("--") for o in opts):
                    bad.append(f"{path} {opts}")
                if not top and "-r" in opts:
                    bad.append(f"{path} {opts}")
            for name, sub in getattr(cmd, "commands", {}).items():
                walk(sub, f"{path} {name}", False)

        walk(_click(app), "ocfl", True)
        assert not bad, bad

    def test_digest_choices_are_the_spec_algorithms(self):
        from ocfl_cli.stage._app import DigestOption
        from ocfl_lib import DigestAlgorithm

        for option in DigestOption:
            assert DigestAlgorithm.from_ocfl_name(option.value).is_spec_algorithm
        assert {o.value for o in DigestOption} == {"sha512", "sha256"}

    def test_batch_ids_resolve_in_one_place(self):
        text = (CLI_SRC / "workflow/batch/report.py").read_text()
        assert "def resolve_batch_id(" in text
        assert "resolve_batch_id(staging_root, batch_id)" in text


class TestStillPending:
    def test_only_the_upgrade_commands_exit_75(self):
        homes = set()
        for p in _py_files(CLI_SRC):
            tree = _tree(p)
            for fn in ast.walk(tree):
                if not isinstance(fn, ast.FunctionDef):
                    continue
                for n in ast.walk(fn):
                    if isinstance(n, ast.Attribute) and n.attr == "PENDING":
                        homes.add(f"{_rel(p)}:{fn.name}")
        assert homes == {
            "stage/timestamp.py:upgrade_cmd",
            "stage/batch.py:batch_upgrade",
        }

    def test_75_is_no_error_class(self):
        from ocfl_cli.exit_codes import ExitCode, exit_code_for_class
        from ocfl_lib import ErrorClass

        assert ExitCode.PENDING == 75
        assert ExitCode.PENDING not in {exit_code_for_class(c) for c in ErrorClass}
