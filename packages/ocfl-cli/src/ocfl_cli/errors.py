"""Typed exception hierarchy for ocfl_cli.

Single-rooted at CLIError. Never imports from ocfl_lib.errors.

The `Transient` marker mixin signals a retriable fault to `_retry.retry`.
Concrete exception classes that mix it in (e.g. `SimpleProofTransient`)
may set per-instance `retry_after` to override the backoff schedule.
"""


class CLIError(Exception):
    """Base class for all ocfl_cli raised exceptions."""


class ConfigError(CLIError):
    """Raised when the user config file is malformed or invalid."""


class StagingError(CLIError):
    """Base class for staging-directory faults."""


class PhaseError(StagingError):
    """Command is not legal in the staging directory's current phase."""


class LockTimeoutError(StagingError):
    """Could not acquire the staging lock within the allowed timeout."""


class OTSError(CLIError):
    """Base class for OpenTimestamps-related faults."""


class Transient:
    """Marker mixin for retriable exceptions.

    `retry_after` is a class-level default; per-instance overrides are set
    at raise time (e.g., `exc.retry_after = float(resp.headers["Retry-After"])`).
    When set, the retry helper sleeps exactly that many seconds with no
    jitter, ignoring the configured backoff for that attempt.
    """

    retry_after: float | None = None


class SimpleProofError(OTSError):
    """Base class for SimpleProof API faults."""


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


class BitcoinUnavailable(BitcoinError):
    """Bitcoin node unreachable after retries — fail fast."""


class AttestationError(OTSError):
    """Base class for attestation-verification faults."""


class ReorgDetectedError(AttestationError):
    """Reorg deeper than confirmation depth — HALT for operator review."""


class AttestationMismatchError(AttestationError):
    """Attestation merkle path does not match local chain — HALT."""
