from ocfl_lib import ErrorClass


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


class InventoryProofMissingError(StagingError):
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


class Transient(Exception):
    def __init__(self, *args: object, retry_after: float | None = None) -> None:
        super().__init__(*args)
        self.retry_after = retry_after


class SimpleProofError(OTSError):
    code = "simpleproof_error"
    error_class = ErrorClass.SERVICE


class SimpleProofAuthError(SimpleProofError):
    code = "simpleproof_auth"
    error_class = ErrorClass.CONFIG


class SimpleProofBadRequest(SimpleProofError):
    code = "simpleproof_bad_request"
    error_class = ErrorClass.SERVICE


class SimpleProofTransient(Transient, SimpleProofError):
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
