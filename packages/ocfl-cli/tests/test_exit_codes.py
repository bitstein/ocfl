"""Canonical CLI exit-code contract."""

import ast
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from ocfl_cli._exit import exit_for_error
from ocfl_cli.errors import (
    AttestationMismatchError,
    CLIError,
    CommitFailedError,
    ConfigError,
    InventoryProofMissingError,
    LockTimeoutError,
    PhaseError,
    ReorgDetectedError,
)
from ocfl_cli.exit_codes import ExitCode, exit_code_for
from ocfl_cli.main import app

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "ocfl_cli"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (CLIError("generic"), ExitCode.GENERIC),
        (PhaseError("wrong phase"), ExitCode.PHASE),
        (LockTimeoutError("busy"), ExitCode.LOCK_TIMEOUT),
        (AttestationMismatchError("mismatch"), ExitCode.ATTESTATION),
        (ReorgDetectedError("reorg"), ExitCode.ATTESTATION),
        (ConfigError("bad config"), ExitCode.CONFIG),
        (CommitFailedError("failed"), ExitCode.GENERIC),
        (CommitFailedError("interrupted", interrupted=True), ExitCode.INTERRUPTED),
        (InventoryProofMissingError("missing proof"), ExitCode.PHASE),
        (KeyboardInterrupt(), ExitCode.INTERRUPTED),
    ],
)
def test_exit_code_mapping_is_canonical(exc: BaseException, expected: ExitCode) -> None:
    assert exit_code_for(exc) is expected


def test_exit_for_error_renders_and_exits_with_canonical_code(capsys) -> None:
    with pytest.raises(typer.Exit) as exc_info:
        exit_for_error(ConfigError("bad config"))

    captured = capsys.readouterr()
    assert exc_info.value.exit_code == int(ExitCode.CONFIG)
    assert "Error: bad config" in captured.err


def test_root_config_error_exits_6(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text("not [valid")
    monkeypatch.setenv("OCFL_CONFIG", str(config))

    result = runner.invoke(app, ["validate"])

    assert result.exit_code == 6, result.output
    assert str(config) in result.output


def test_command_handlers_do_not_bypass_canonical_error_exit() -> None:
    offenders = []
    for path in SRC_ROOT.rglob("*.py"):
        if path.name == "_exit.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler) or node.name is None:
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and _is_caught_exception_render(
                    child, node.name
                ):
                    offenders.append(
                        f"{path.relative_to(SRC_ROOT).as_posix()}:{child.lineno}"
                    )

    assert offenders == []


def _is_caught_exception_render(node: ast.Call, exception_name: str) -> bool:
    if not isinstance(node.func, ast.Name) or node.func.id != "err":
        return False
    if len(node.args) != 1:
        return False
    arg = node.args[0]
    return _is_str_of_name(arg, exception_name) or _contains_formatted_name(
        arg, exception_name
    )


def _is_str_of_name(node: ast.AST, name: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "str"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == name
    )


def _contains_formatted_name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.JoinedStr) and any(
        isinstance(value, ast.FormattedValue)
        and isinstance(value.value, ast.Name)
        and value.value.id == name
        for value in node.values
    )
