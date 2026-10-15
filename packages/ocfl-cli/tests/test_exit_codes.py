import json
from pathlib import Path

import pytest
import typer
from runner_helpers import ObjRunner

from ocfl_cli import _exit
from ocfl_cli._exit import error_class_of, error_code_of, handle_error
from ocfl_cli.errors import (
    AttestationMismatchError,
    CLIError,
    CommitClaimLostError,
    CommitFailedError,
    CommitInterruptedError,
    ConfigError,
    ErrorContext,
    InventoryProofMissingError,
    LockTimeoutError,
    PhaseError,
    ReorgDetectedError,
    SimpleProofAuthError,
    SimpleProofTransient,
    UsageError,
)
from ocfl_cli.exit_codes import ExitCode, exit_code_for_class
from ocfl_cli.main import app
from ocfl_lib import (
    BitcoinRpcAuthError,
    BitcoinRpcTransientError,
    ErrorClass,
    ObjectLockedError,
    ObjectNotFound,
)


@pytest.mark.parametrize(
    ("error_class", "expected"),
    [
        (ErrorClass.USAGE, ExitCode.USAGE),
        (ErrorClass.NOT_FOUND, ExitCode.GENERIC),
        (ErrorClass.INVALID, ExitCode.GENERIC),
        (ErrorClass.SERVICE, ExitCode.GENERIC),
        (ErrorClass.STATE, ExitCode.PHASE),
        (ErrorClass.BUSY, ExitCode.LOCK_TIMEOUT),
        (ErrorClass.HALT, ExitCode.ATTESTATION),
        (ErrorClass.CONFIG, ExitCode.CONFIG),
        (ErrorClass.INTERRUPTED, ExitCode.INTERRUPTED),
    ],
)
def test_class_to_exit_code_follows_adr_0004(
    error_class: ErrorClass, expected: ExitCode
) -> None:
    assert exit_code_for_class(error_class) is expected


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (CLIError("generic"), ExitCode.GENERIC),
        (UsageError("bad flags"), ExitCode.USAGE),
        (PhaseError("wrong phase"), ExitCode.PHASE),
        (LockTimeoutError("busy"), ExitCode.LOCK_TIMEOUT),
        (AttestationMismatchError("mismatch"), ExitCode.ATTESTATION),
        (ReorgDetectedError("reorg"), ExitCode.ATTESTATION),
        (ConfigError("bad config"), ExitCode.CONFIG),
        (CommitFailedError("failed"), ExitCode.GENERIC),
        (CommitInterruptedError("interrupted"), ExitCode.INTERRUPTED),
        (CommitClaimLostError("lost"), ExitCode.PHASE),
        (InventoryProofMissingError("missing proof"), ExitCode.PHASE),
        (KeyboardInterrupt(), ExitCode.INTERRUPTED),
        (OSError("disk"), ExitCode.GENERIC),
        (ObjectNotFound("o"), ExitCode.GENERIC),
        (ObjectLockedError("o"), ExitCode.LOCK_TIMEOUT),
        (BitcoinRpcTransientError("down"), ExitCode.GENERIC),
        (BitcoinRpcAuthError("401"), ExitCode.CONFIG),
        (SimpleProofAuthError("401"), ExitCode.CONFIG),
        (SimpleProofTransient("503"), ExitCode.GENERIC),
        (
            ErrorContext(ReorgDetectedError("reorg"), prefix="halt: "),
            ExitCode.ATTESTATION,
        ),
    ],
)
def test_error_exit_codes(exc: BaseException, expected: ExitCode) -> None:
    assert exit_code_for_class(error_class_of(exc)) is expected


def test_context_keeps_the_cause_code_and_adds_to_the_message() -> None:
    exc = ErrorContext(ObjectNotFound("o"), prefix="before: ", suffix="; after")
    assert error_code_of(exc) == "object_not_found"
    assert str(exc) == "before: object 'o' not found; after"


def test_handle_error_text(capsys) -> None:
    with pytest.raises(typer.Exit) as exc_info:
        handle_error(ConfigError("bad config"), json_mode=False)

    captured = capsys.readouterr()
    assert exc_info.value.exit_code == int(ExitCode.CONFIG)
    assert captured.out == ""
    assert "Error: bad config" in captured.err


def test_handle_error_json(capsys) -> None:
    with pytest.raises(typer.Exit) as exc_info:
        handle_error(LockTimeoutError("busy"), json_mode=True)

    captured = capsys.readouterr()
    assert exc_info.value.exit_code == int(ExitCode.LOCK_TIMEOUT)
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error": {"code": "lock_timeout", "message": "busy", "exit": 4}
    }


def test_exit_module_documents_the_json_error_shape(capsys) -> None:
    with pytest.raises(typer.Exit):
        handle_error(LockTimeoutError("busy"), json_mode=True)
    keys = json.loads(capsys.readouterr().err)["error"]
    shape = '{"error": {' + ", ".join(f'"{k}"' for k in keys) + "}}"

    assert shape in (_exit.__doc__ or "")


def test_root_config_error_exits_6(
    runner: ObjRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text("not [valid")
    monkeypatch.setenv("OCFL_CONFIG", str(config))

    result = runner.invoke(app, ["validate"])

    assert result.exit_code == ExitCode.CONFIG, result.output
    assert str(config) in result.output


def test_root_config_error_json(
    runner: ObjRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text("not [valid")
    monkeypatch.setenv("OCFL_CONFIG", str(config))

    result = runner.invoke(app, ["--json", "validate"])

    assert result.exit_code == ExitCode.CONFIG, result.output
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "config_error" and error["exit"] == 6
    assert str(config) in error["message"]


def test_missing_object_json_error_on_stderr(runner: ObjRunner, tmp_path: Path) -> None:
    from ocfl_lib import LocalFS, Spec, StorageRoot, get_layout

    fs = LocalFS(tmp_path)
    StorageRoot.init(
        fs, fs, spec=Spec.V1_1, layout=get_layout("0002-flat-direct-storage-layout")
    )

    result = runner.invoke(app, ["--json", "--root", str(tmp_path), "info", "nope"])

    assert result.exit_code == ExitCode.GENERIC, result.output
    assert result.stdout == ""
    assert json.loads(result.stderr) == {
        "error": {
            "code": "object_not_found",
            "message": "object 'nope' not found",
            "exit": 1,
        }
    }
