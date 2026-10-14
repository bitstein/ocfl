from typing import Literal

import msgspec

from ocfl_cli.model.phase import TimestampTarget  # noqa: TC001 - msgspec decodes it.
from ocfl_cli.model.simpleproof import VerifyJobStatus  # noqa: TC001 - msgspec decodes it.

BATCH_FILE_SCHEMA_VERSION = 1


BatchObjectStatus = Literal["prepared", "submitted", "upgraded", "failed", "skipped"]
BatchSubmissionStatus = Literal["pending", "submitted", "failed"]


class BatchObject(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    object_id: str
    status: BatchObjectStatus
    digests: tuple[str, ...] = ()
    skipped_reason: str | None = None
    last_error: str | None = None


class BatchSubmission(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    chunk_index: int
    digests: tuple[str, ...]
    status: BatchSubmissionStatus
    record_ids: tuple[str, ...] | None = None
    submitted_at: str | None = None
    attempts: int = 0
    last_error: str | None = None


class BatchVerifyJob(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    job_id: str
    digests: tuple[str, ...]
    status: VerifyJobStatus
    submitted_at: str
    results_url: str | None = None
    results_url_expires_at: str | None = None
    attempts: int = 0
    completed_at: str | None = None
    last_error: str | None = None


class BatchFile(msgspec.Struct, frozen=True, kw_only=True, forbid_unknown_fields=True):
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
