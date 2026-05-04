"""Canonical CLI exit-code contract."""

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from ocfl_cli._exit import exit_for_error
from ocfl_cli.errors import (
    AttestationMismatchError,
    CLIError,
    ConfigError,
    LockTimeoutError,
    PhaseError,
    ReorgDetectedError,
)
from ocfl_cli.exit_codes import ExitCode, exit_code_for
from ocfl_cli.main import app


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (CLIError("generic"), ExitCode.GENERIC),
        (PhaseError("wrong phase"), ExitCode.PHASE),
        (LockTimeoutError("busy"), ExitCode.LOCK_TIMEOUT),
        (AttestationMismatchError("mismatch"), ExitCode.ATTESTATION),
        (ReorgDetectedError("reorg"), ExitCode.ATTESTATION),
        (ConfigError("bad config"), ExitCode.CONFIG),
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
