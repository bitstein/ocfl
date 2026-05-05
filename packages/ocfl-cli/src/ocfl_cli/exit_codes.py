"""Canonical process exit codes for the CLI."""

from dataclasses import dataclass
from enum import IntEnum

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


@dataclass(frozen=True, slots=True)
class ExitCodeDoc:
    code: ExitCode
    description: str


EXIT_CODE_DOCS: tuple[ExitCodeDoc, ...] = (
    ExitCodeDoc(ExitCode.SUCCESS, "success"),
    ExitCodeDoc(ExitCode.GENERIC, "generic CLI/domain error"),
    ExitCodeDoc(ExitCode.USAGE, "usage error"),
    ExitCodeDoc(ExitCode.PHASE, "staging phase error"),
    ExitCodeDoc(ExitCode.LOCK_TIMEOUT, "staging lock timeout; retry with backoff"),
    ExitCodeDoc(
        ExitCode.ATTESTATION,
        "attestation mismatch or Bitcoin reorg; retry after re-fetch",
    ),
    ExitCodeDoc(ExitCode.CONFIG, "configuration error"),
    ExitCodeDoc(ExitCode.INTERRUPTED, "interrupted"),
)


def format_exit_code_help() -> str:
    """Return user-facing help text for the canonical process exits."""
    lines = ["Exit codes:"]
    lines.extend(f"  {int(doc.code)}  {doc.description}" for doc in EXIT_CODE_DOCS)
    return "\n".join(lines)


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
    if isinstance(exc, CommitFailedError) and exc.interrupted:
        return ExitCode.INTERRUPTED
    if isinstance(exc, InventoryProofMissingError):
        return ExitCode.PHASE
    if isinstance(exc, PhaseError):
        return ExitCode.PHASE
    if isinstance(exc, CLIError):
        return ExitCode.GENERIC
    return ExitCode.GENERIC
