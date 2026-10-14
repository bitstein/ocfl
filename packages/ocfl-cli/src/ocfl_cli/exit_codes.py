from dataclasses import dataclass
from enum import IntEnum
from typing import assert_never

from ocfl_lib import ErrorClass


class ExitCode(IntEnum):
    SUCCESS = 0
    GENERIC = 1
    USAGE = 2
    PHASE = 3
    LOCK_TIMEOUT = 4
    ATTESTATION = 5
    CONFIG = 6
    PENDING = 75
    INTERRUPTED = 130


@dataclass(frozen=True, slots=True)
class ExitCodeDoc:
    code: ExitCode
    description: str


EXIT_CODE_DOCS: tuple[ExitCodeDoc, ...] = (
    ExitCodeDoc(ExitCode.SUCCESS, "success"),
    ExitCodeDoc(ExitCode.GENERIC, "error: not found, invalid, or a service failed"),
    ExitCodeDoc(ExitCode.USAGE, "usage error"),
    ExitCodeDoc(
        ExitCode.PHASE,
        "state error: wrong staging phase, claim lost, or recovery needed",
    ),
    ExitCodeDoc(ExitCode.LOCK_TIMEOUT, "busy: a lock is held; retry with backoff"),
    ExitCodeDoc(
        ExitCode.ATTESTATION,
        "halt: attestation mismatch or deep Bitcoin reorg; operator review",
    ),
    ExitCodeDoc(ExitCode.CONFIG, "configuration error, including credentials"),
    ExitCodeDoc(
        ExitCode.PENDING, "still pending: proofs not final yet; run again later"
    ),
    ExitCodeDoc(ExitCode.INTERRUPTED, "interrupted"),
)


def format_exit_code_help() -> str:
    lines = ["Exit codes:"]
    lines.extend(f"  {int(doc.code)}  {doc.description}" for doc in EXIT_CODE_DOCS)
    return "\n".join(lines)


def exit_code_for_class(error_class: ErrorClass) -> ExitCode:
    match error_class:
        case ErrorClass.NOT_FOUND | ErrorClass.INVALID | ErrorClass.SERVICE:
            return ExitCode.GENERIC
        case ErrorClass.USAGE:
            return ExitCode.USAGE
        case ErrorClass.STATE:
            return ExitCode.PHASE
        case ErrorClass.BUSY:
            return ExitCode.LOCK_TIMEOUT
        case ErrorClass.HALT:
            return ExitCode.ATTESTATION
        case ErrorClass.CONFIG:
            return ExitCode.CONFIG
        case ErrorClass.INTERRUPTED:
            return ExitCode.INTERRUPTED
        case _:
            assert_never(error_class)
