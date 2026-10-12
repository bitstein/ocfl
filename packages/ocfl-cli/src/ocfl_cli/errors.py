"""Typed exception hierarchy for ocfl_cli.

Single-rooted at CLIError. Never imports from ocfl_lib._errors.

The `Transient` marker mixin signals a retriable fault to `ots._retry.retry`.
Concrete exception classes that mix it in (e.g. `SimpleProofTransient`)
may set per-instance `retry_after` to override the backoff schedule.

`SimpleProofError.partial_result` follows the same per-instance pattern:
when a multi-chunk submit fails partway, the raise site attaches a
`SubmitResult` aggregating chunks that succeeded before the failure.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ocfl_cli.ots.simpleproof import SubmitResult


class CLIError(Exception):
    """Base class for all ocfl_cli raised exceptions."""


class ConfigError(CLIError):
    """Raised when the user config file is malformed or invalid."""


class StagingError(CLIError):
    """Base class for staging-directory faults."""


class StagingFileMissingError(StagingError):
    """A content map entry refers to a missing source file."""


class StagingFileChangedError(StagingError):
    """A content map entry's source file changed since staging."""


class PhaseError(StagingError):
    """Command is not legal in the staging directory's current phase."""


class InventoryProofMissingError(PhaseError):
    """Inventory retry requested without a persisted inventory proof."""


class LockTimeoutError(StagingError):
    """Could not acquire the staging lock within the allowed timeout."""


class MissingStagedMetadataError(StagingError):
    """Required staged version metadata is absent."""


class CommitFailedError(StagingError):
    """The commit failed; staging was preserved for retry.

    `claim_lost` means another writer claimed the version first and this
    commit wrote nothing into the object.
    """

    def __init__(
        self, message: str, *, interrupted: bool = False, claim_lost: bool = False
    ) -> None:
        super().__init__(message)
        self.interrupted = interrupted
        self.claim_lost = claim_lost


class OTSError(CLIError):
    """Base class for OpenTimestamps-related faults."""


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

    partial_result: SubmitResult | None = None


class SimpleProofAuthError(SimpleProofError):
    """401/403 from SimpleProof — fail fast, operator must fix credentials."""


class SimpleProofBadRequest(SimpleProofError):
    """400/422 from SimpleProof — fail fast, indicates a bug or bad config."""


class SimpleProofTransient(SimpleProofError, Transient):
    """5xx/429/timeout/connection error from SimpleProof — retriable."""


class BitcoinError(OTSError):
    """Base class for Bitcoin RPC / ZMQ faults."""


class BitcoinAuthError(BitcoinError):
    """Bitcoin RPC authentication failure — fail fast."""


class BitcoinRPCTransient(BitcoinError, Transient):
    """Transient RPC/ZMQ error — retriable."""


class BitcoinBlockMissingError(BitcoinError):
    """Requested block is absent from the node's active chain."""


class BitcoinUnavailable(BitcoinError):
    """Bitcoin node unreachable after retries — fail fast."""


class AttestationError(OTSError):
    """Base class for attestation-verification faults."""


class ReorgDetectedError(AttestationError):
    """Reorg deeper than confirmation depth — HALT for operator review."""


class AttestationMismatchError(AttestationError):
    """Attestation merkle path does not match local chain — HALT."""
