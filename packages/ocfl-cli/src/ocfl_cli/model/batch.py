"""Batch timestamping state: the batch file and driver lease model."""

from typing import Literal

import msgspec

from ocfl_cli.model.phase import TimestampTarget  # noqa: TC001 - msgspec decodes it.

BATCH_FILE_SCHEMA_VERSION = 1


BatchObjectStatus = Literal["prepared", "submitted", "upgraded", "failed", "skipped"]
BatchSubmissionStatus = Literal["pending", "submitted", "failed"]
BatchVerifyJobStatus = Literal[
    "PENDING", "IN_PROGRESS", "PROCESSING", "COMPLETED", "FAILED", "EXPIRED"
]
BATCH_VERIFY_ACTIVE_STATUSES = frozenset({"PENDING", "IN_PROGRESS", "PROCESSING"})
BATCH_VERIFY_JOB_STATUSES = frozenset(
    {*BATCH_VERIFY_ACTIVE_STATUSES, "COMPLETED", "FAILED", "EXPIRED"}
)
BATCH_VERIFY_TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "EXPIRED"})


class BatchObject(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One object row in a batch file."""

    object_id: str
    status: BatchObjectStatus
    digests: tuple[str, ...] = ()
    skipped_reason: str | None = None
    last_error: str | None = None


class BatchSubmission(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One SimpleProof submit chunk tracked by a batch file."""

    chunk_index: int
    digests: tuple[str, ...]
    status: BatchSubmissionStatus
    record_ids: tuple[str, ...] | None = None
    submitted_at: str | None = None
    attempts: int = 0
    last_error: str | None = None


class BatchVerifyJob(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One SimpleProof verify-batches job tracked by a batch file."""

    job_id: str
    digests: tuple[str, ...]
    status: BatchVerifyJobStatus
    submitted_at: str
    results_url: str | None = None
    results_url_expires_at: str | None = None
    attempts: int = 0
    completed_at: str | None = None
    last_error: str | None = None


class BatchFile(msgspec.Struct, frozen=True, kw_only=True, forbid_unknown_fields=True):
    """On-disk batch file schema."""

    schema_version: int = BATCH_FILE_SCHEMA_VERSION
    batch_id: str
    target: TimestampTarget
    root_path: str
    category_id: int
    created_at: str
    updated_at: str
    objects: tuple[BatchObject, ...] = ()
    submissions: tuple[BatchSubmission, ...] = ()
    verify_jobs: tuple[BatchVerifyJob, ...] = ()


class DriverLease(
    msgspec.Struct, frozen=True, kw_only=True, forbid_unknown_fields=True
):
    """One command-driver lease holder for a batch."""

    batch_id: str
    pid: int
    host: str
    started_at: str
    heartbeat_at: str
    command: str
    cli_version: str
