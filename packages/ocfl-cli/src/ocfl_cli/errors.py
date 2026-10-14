"""Typed exception hierarchy for ocfl_cli.

Single-rooted at CLIError. Like the lib's errors, every class carries an
`ErrorClass` (how a caller should react, which picks the exit code) and a
stable snake_case `code` (part of the `--json` error contract). Bitcoin RPC
errors are the lib's own (`ocfl_lib.BitcoinRpcError` and subclasses).

The `Transient` marker mixin signals a retriable fault to `ots._retry.retry`.
Concrete exception classes that mix it in (e.g. `SimpleProofTransient`)
may set per-instance `retry_after` to override the backoff schedule.

`SimpleProofError.partial_result` follows the same per-instance pattern:
when a multi-chunk submit fails partway, the raise site attaches a
`SubmitResult` aggregating chunks that succeeded before the failure.
"""

from typing import TYPE_CHECKING

from ocfl_lib import ErrorClass

if TYPE_CHECKING:
    from ocfl_cli.model.simpleproof import SubmitResult


class CLIError(Exception):
    code: str = "cli_error"
    error_class: ErrorClass = ErrorClass.INVALID


class UsageError(CLIError):
    code = "usage_error"
    error_class = ErrorClass.USAGE


class ErrorContext(CLIError):
    code = "error_context"
    error_class = ErrorClass.INVALID

    def __init__(self, cause: Exception, *, prefix: str = "", suffix: str = "") -> None:
        super().__init__(f"{prefix}{cause}{suffix}")
        self.cause = cause


class ConfigError(CLIError):
    code = "config_error"
    error_class = ErrorClass.CONFIG


class ConfigCheckFailedError(CLIError):
    code = "config_check_failed"
    error_class = ErrorClass.CONFIG


class ServiceCheckFailedError(CLIError):
    code = "service_check_failed"
    error_class = ErrorClass.SERVICE


class StagingError(CLIError):
    code = "staging_error"
    error_class = ErrorClass.INVALID


class StagingFileMissingError(StagingError):
    code = "staging_file_missing"
    error_class = ErrorClass.INVALID


class StagingFileChangedError(StagingError):
    code = "staging_file_changed"
    error_class = ErrorClass.INVALID


class BatchFileMissingError(StagingError):
    code = "batch_file_missing"
    error_class = ErrorClass.NOT_FOUND


class BatchMismatchError(StagingError):
    code = "batch_mismatch"
    error_class = ErrorClass.STATE


class ObjectsNotStagedError(StagingError):
    code = "objects_not_staged"
    error_class = ErrorClass.NOT_FOUND


class PhaseError(StagingError):
    code = "phase_error"
    error_class = ErrorClass.STATE


class InventoryProofMissingError(PhaseError):
    """The staged inventory has no final proof yet, or its plan or cached
    proof is missing: run the timestamp workflow again (not a halt)."""

    code = "inventory_proof_missing"
    error_class = ErrorClass.STATE


class LockTimeoutError(StagingError):
    code = "lock_timeout"
    error_class = ErrorClass.BUSY


class MissingStagedMetadataError(StagingError):
    code = "missing_staged_metadata"
    error_class = ErrorClass.INVALID


class CommitFailedError(StagingError):
    code = "commit_failed"
    error_class = ErrorClass.INVALID


class CommitClaimLostError(CommitFailedError):
    code = "commit_claim_lost"
    error_class = ErrorClass.STATE


class CommitInterruptedError(CommitFailedError):
    code = "commit_interrupted"
    error_class = ErrorClass.INTERRUPTED


class OTSError(CLIError):
    code = "ots_error"
    error_class = ErrorClass.SERVICE


class Transient:
    """Marker mixin for retriable exceptions.

    `retry_after` is a class-level default; per-instance overrides are set
    at raise time (e.g., `exc.retry_after = float(resp.headers["Retry-After"])`).
    When set, the retry helper sleeps exactly that many seconds with no
    configured backoff for that attempt.
    """

    retry_after: float | None = None


class SimpleProofError(OTSError):
    """Base class for SimpleProof API faults.

    `partial_result` is a class-level default; per-instance overrides are
    set at raise time when a multi-chunk `submit_hashes` fails partway
    (e.g., `exc.partial_result = SubmitResult(...)`). Callers inspect this
    to recover digests/record_ids that did make it to SP before retrying.
    """

    code = "simpleproof_error"
    error_class = ErrorClass.SERVICE
    partial_result: SubmitResult | None = None


class SimpleProofAuthError(SimpleProofError):
    code = "simpleproof_auth"
    error_class = ErrorClass.CONFIG


class SimpleProofBadRequest(SimpleProofError):
    code = "simpleproof_bad_request"
    error_class = ErrorClass.SERVICE


class SimpleProofTransient(SimpleProofError, Transient):
    code = "simpleproof_transient"
    error_class = ErrorClass.SERVICE


class AttestationError(OTSError):
    code = "attestation_error"
    error_class = ErrorClass.HALT


class ReorgDetectedError(AttestationError):
    code = "reorg_detected"
    error_class = ErrorClass.HALT


class AttestationMismatchError(AttestationError):
    code = "attestation_mismatch"
    error_class = ErrorClass.HALT
