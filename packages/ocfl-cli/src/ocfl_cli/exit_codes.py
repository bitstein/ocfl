"""Canonical process exit codes for the CLI."""

from enum import IntEnum

from ocfl_cli.errors import (
    AttestationMismatchError,
    CLIError,
    ConfigError,
    LockTimeoutError,
    PhaseError,
    ReorgDetectedError,
)


class ExitCode(IntEnum):
    SUCCESS = 0
    GENERIC = 1
    # Click emits this automatically for parse errors. CLI code may also use it
    # for post-parse invocation-shape validation, such as incompatible flags.
    USAGE = 2
    PHASE = 3
    LOCK_TIMEOUT = 4
    ATTESTATION = 5
    CONFIG = 6
    INTERRUPTED = 130


def exit_code_for(exc: BaseException) -> ExitCode:
    """Return the canonical exit code for a known CLI exception."""
    if isinstance(exc, KeyboardInterrupt):
        return ExitCode.INTERRUPTED
    if isinstance(exc, ConfigError):
        return ExitCode.CONFIG
    if isinstance(exc, (AttestationMismatchError, ReorgDetectedError)):
        return ExitCode.ATTESTATION
    if isinstance(exc, LockTimeoutError):
        return ExitCode.LOCK_TIMEOUT
    if isinstance(exc, PhaseError):
        return ExitCode.PHASE
    if isinstance(exc, CLIError):
        return ExitCode.GENERIC
    return ExitCode.GENERIC
