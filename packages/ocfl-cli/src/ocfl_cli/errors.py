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
    """Base class for all ocfl_cli raised exceptions."""

    code: str = "cli_error"
    error_class: ErrorClass = ErrorClass.INVALID


class UsageError(CLIError):
    """A command line that parses but is still wrong once the CLI checks it."""

    code = "usage_error"
    error_class = ErrorClass.USAGE


class ErrorContext(CLIError):
    """Adds context to another error's message; keeps its class and code."""

    code = "error_context"
    error_class = ErrorClass.INVALID

    def __init__(self, cause: Exception, *, prefix: str = "", suffix: str = "") -> None:
        super().__init__(f"{prefix}{cause}{suffix}")
        self.cause = cause


class ConfigError(CLIError):
    """Raised when the user config file is malformed or invalid."""

    code = "config_error"
    error_class = ErrorClass.CONFIG


class ConfigCheckFailedError(CLIError):
    """`config check`: the config or a credential does not work."""

    code = "config_check_failed"
    error_class = ErrorClass.CONFIG


class ServiceCheckFailedError(CLIError):
    """`config check`: a configured service cannot be reached."""

    code = "service_check_failed"
    error_class = ErrorClass.SERVICE


class StagingError(CLIError):
    """Base class for staging-directory faults."""

    code = "staging_error"
    error_class = ErrorClass.INVALID


class StagingFileMissingError(StagingError):
    """A content map entry refers to a missing source file."""

    code = "staging_file_missing"
    error_class = ErrorClass.INVALID


class StagingFileChangedError(StagingError):
    """A content map entry's source file changed since staging."""

    code = "staging_file_changed"
    error_class = ErrorClass.INVALID


class BatchFileMissingError(StagingError):
    """No batch file exists for the requested batch id."""

    code = "batch_file_missing"
    error_class = ErrorClass.USAGE


class PhaseError(StagingError):
    """Command is not legal in the staging directory's current phase."""

    code = "phase_error"
    error_class = ErrorClass.STATE


class InventoryProofMissingError(PhaseError):
    """Inventory retry requested without a persisted inventory proof."""

    code = "inventory_proof_missing"
    error_class = ErrorClass.STATE


class LockTimeoutError(StagingError):
    """Could not acquire the staging lock within the allowed timeout."""

    code = "lock_timeout"
    error_class = ErrorClass.BUSY


class MissingStagedMetadataError(StagingError):
    """Required staged version metadata is absent."""

    code = "missing_staged_metadata"
    error_class = ErrorClass.INVALID


class CommitFailedError(StagingError):
    """The commit failed; staging was preserved for retry."""

    code = "commit_failed"
    error_class = ErrorClass.INVALID


class CommitClaimLostError(CommitFailedError):
    """Another writer claimed the version first; nothing was written."""

    code = "commit_claim_lost"
    error_class = ErrorClass.STATE


class CommitInterruptedError(CommitFailedError):
    """The commit was interrupted and reverted."""

    code = "commit_interrupted"
    error_class = ErrorClass.INTERRUPTED


class OTSError(CLIError):
    """Base class for OpenTimestamps-related faults."""

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
    """401/403 from SimpleProof — fail fast, operator must fix credentials."""

    code = "simpleproof_auth"
    error_class = ErrorClass.CONFIG


class SimpleProofBadRequest(SimpleProofError):
    """400/422 from SimpleProof — fail fast, indicates a bug or bad config."""

    code = "simpleproof_bad_request"
    error_class = ErrorClass.SERVICE


class SimpleProofTransient(SimpleProofError, Transient):
    """5xx/429/timeout/connection error from SimpleProof — retriable."""

    code = "simpleproof_transient"
    error_class = ErrorClass.SERVICE


class AttestationError(OTSError):
    """Base class for attestation-verification faults."""

    code = "attestation_error"
    error_class = ErrorClass.HALT


class ReorgDetectedError(AttestationError):
    """Reorg deeper than confirmation depth — HALT for operator review."""

    code = "reorg_detected"
    error_class = ErrorClass.HALT


class AttestationMismatchError(AttestationError):
    """Attestation merkle path does not match local chain — HALT."""

    code = "attestation_mismatch"
    error_class = ErrorClass.HALT
