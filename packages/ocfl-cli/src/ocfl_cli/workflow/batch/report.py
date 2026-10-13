"""Batch read models: status, events and counts in their `--json` shapes."""

from pathlib import Path  # noqa: TC003 - Typer reads option annotations at runtime.
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict

from ocfl_cli import errors as errors
from ocfl_cli.errors import (
    BatchFileMissingError,
    CLIError,
    StagingError,
)
from ocfl_cli.model import batch as batch_model
from ocfl_cli.model.ots_state import (  # noqa: TC001 - get_type_hints reads it.
    OtsState,
)
from ocfl_cli.model.phase import (
    TimestampTarget,  # noqa: TC001 - get_type_hints reads it.
)
from ocfl_cli.store import batch as batch_state
from ocfl_cli.store.ots_state import (
    has_ots,
    load_ots,
)
from ocfl_cli.store.staging import StagingStore
from ocfl_cli.workflow import hints
from ocfl_cli.workflow.batch.reasons import BATCH_MISSING_NEXT, reason_next

if TYPE_CHECKING:
    from ocfl_cli.ports import Runtime

BATCH_STATUSES = ("prepared", "submitted", "upgraded", "failed", "skipped")


class BatchCountsOutput(TypedDict):
    prepared: int
    submitted: int
    upgraded: int
    failed: int
    skipped: int
    total: int


class BatchVerboseDigestOutput(TypedDict):
    digest: str
    submitted: bool
    verified: bool
    record_id: str | None
    submit_status: batch_model.BatchSubmissionStatus | None
    verify_status: batch_model.BatchVerifyJobStatus | None


class BatchStatusObjectOutput(TypedDict):
    object_id: str
    phase: str | None
    batch_status: batch_model.BatchObjectStatus
    digests_total: int
    digests_verified: int
    digests_submitted: int
    last_error: str | None
    skipped_reason: str | None
    next: str
    batch_id: str
    digests: NotRequired[list[BatchVerboseDigestOutput]]


class BatchSubmissionOutput(TypedDict):
    chunk_index: int
    digests: list[str]
    status: batch_model.BatchSubmissionStatus
    record_ids: list[str] | None
    submitted_at: str | None
    attempts: int
    last_error: str | None


class BatchVerifyJobOutput(TypedDict):
    job_id: str
    digests: list[str]
    status: batch_model.BatchVerifyJobStatus
    submitted_at: str
    results_url: str | None
    results_url_expires_at: str | None
    attempts: int
    completed_at: str | None
    last_error: str | None


class BatchStatusOutput(TypedDict):
    batch_id: str
    target: TimestampTarget
    root_path: str
    category_id: int
    created_at: str
    updated_at: str
    counts: BatchCountsOutput
    objects: list[BatchStatusObjectOutput]
    submissions: list[BatchSubmissionOutput]
    verify_jobs: list[BatchVerifyJobOutput]


type BatchObjectEventName = Literal[
    "object_prepared",
    "object_submitted",
    "object_upgraded",
    "object_skipped",
    "object_failed",
]


class BatchObjectEventOutput(TypedDict):
    event: BatchObjectEventName
    batch_id: str
    target: TimestampTarget
    object_id: str
    status: batch_model.BatchObjectStatus
    digests_total: int
    digests_submitted: int
    digests_verified: int
    last_error: str | None
    skipped_reason: str | None
    next: str


class BatchSummaryEventOutput(TypedDict):
    event: Literal["summary"]
    batch_id: str
    target: TimestampTarget
    category_id: int
    root_path: str
    counts: BatchCountsOutput


type CommitObjectEventName = Literal[
    "object_committed",
    "object_skipped",
    "object_failed",
]


class CommitObjectEventOutput(TypedDict):
    event: CommitObjectEventName
    batch_id: str
    target: TimestampTarget
    object_id: str
    reason: str | None
    message: str | None


class CommitCountsOutput(TypedDict):
    committed: int
    skipped: int
    failed: int
    total: int


class CommitSummaryEventOutput(TypedDict):
    event: Literal["summary"]
    batch_id: str
    target: TimestampTarget
    category_id: int
    root_path: str
    counts: CommitCountsOutput


class BatchCreatedEventOutput(TypedDict):
    event: Literal["batch_created"]
    batch_id: str
    target: TimestampTarget
    category_id: int
    root_path: str
    created_at: str


class BatchResumedEventOutput(TypedDict):
    event: Literal["batch_resumed"]
    batch_id: str
    target: TimestampTarget
    category_id: int
    root_path: str


type BatchEventOutput = (
    BatchObjectEventOutput
    | BatchSummaryEventOutput
    | BatchCreatedEventOutput
    | BatchResumedEventOutput
)


def batch_counts(batch: batch_model.BatchFile) -> BatchCountsOutput:
    counts: BatchCountsOutput = {
        "prepared": 0,
        "submitted": 0,
        "upgraded": 0,
        "failed": 0,
        "skipped": 0,
        "total": len(batch.objects),
    }
    for row in batch.objects:
        counts[row.status] += 1
    return counts


def event_for_row(row: batch_model.BatchObject) -> BatchObjectEventName:
    if row.status == "prepared":
        return "object_prepared"
    if row.status == "submitted":
        return "object_submitted"
    if row.status == "upgraded":
        return "object_upgraded"
    if row.status == "skipped":
        return "object_skipped"
    return "object_failed"


def batch_to_list_row(batch: batch_model.BatchFile) -> dict:
    counts = batch_counts(batch)
    return {
        "batch_id": batch.batch_id,
        "short_batch_id": batch.batch_id.split("-", 1)[0],
        "target": batch.target,
        "category_id": batch.category_id,
        "root_path": batch.root_path,
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
        "counts": counts,
        **counts,
    }


def load_batches_or_exit(staging_root: Path) -> list[batch_model.BatchFile]:
    rows: list[batch_model.BatchFile] = []
    for path in sorted(batch_state.batches_dir(staging_root).glob("*.json")):
        rows.append(batch_state.load_batch(staging_root, path.stem))
    rows.sort(key=lambda batch: batch.updated_at, reverse=True)
    return rows


def load_batch_or_usage(staging_root: Path, batch_id: str) -> batch_model.BatchFile:
    path = batch_state.batch_file_path(staging_root, batch_id)
    if not path.is_file():
        raise BatchFileMissingError(
            f"batch_file_missing: {batch_id}; {BATCH_MISSING_NEXT}"
        )
    return batch_state.load_batch(staging_root, batch_id)


def object_staging_dir(staging_root: Path, *, root_path: str, object_id: str) -> Path:
    return StagingStore(staging_root).staging_path(root_path, object_id)


def load_object_ots(staging_root: Path, batch: batch_model.BatchFile, object_id: str):
    staging_dir = object_staging_dir(
        staging_root, root_path=batch.root_path, object_id=object_id
    )
    if not has_ots(staging_dir):
        return None
    try:
        return load_ots(staging_dir)
    except StagingError:
        return None


def batch_complete(batch: batch_model.BatchFile) -> bool:
    active = [row for row in batch.objects if row.status not in {"skipped", "failed"}]
    return bool(active) and all(row.status == "upgraded" for row in active)


def submitted_count_from_batch(
    row: batch_model.BatchObject, batch: batch_model.BatchFile
) -> int:
    submitted: set[str] = set()
    wanted = set(row.digests)
    for chunk in batch.submissions:
        if chunk.record_ids is None:
            continue
        for digest, record_id in zip(chunk.digests, chunk.record_ids, strict=False):
            if digest in wanted and record_id is not None:
                submitted.add(digest)
    return len(submitted)


def submitted_count_from_object(
    batch: batch_model.BatchFile, row: batch_model.BatchObject, state: OtsState
) -> int:
    ids = object_record_ids_by_digest(batch, row, state)
    return sum(1 for record_id in ids.values() if record_id is not None)


def object_record_ids_by_digest(
    batch: batch_model.BatchFile, row: batch_model.BatchObject, state: OtsState | None
) -> dict[str, str | None]:
    record_ids: dict[str, str | None] = dict.fromkeys(row.digests)
    if state is None:
        return record_ids
    wanted = set(row.digests)
    for submission in state.submissions:
        if (
            submission.batch_id != batch.batch_id
            or submission.target is not batch.target
            or submission.record_ids is None
        ):
            continue
        for digest, record_id in zip(
            submission.digests, submission.record_ids, strict=False
        ):
            if digest in wanted and record_id is not None:
                record_ids[digest] = record_id
    return record_ids


def batch_record_ids_by_digest(
    batch: batch_model.BatchFile, row: batch_model.BatchObject
) -> dict[str, str | None]:
    record_ids: dict[str, str | None] = dict.fromkeys(row.digests)
    wanted = set(row.digests)
    for chunk in batch.submissions:
        if chunk.record_ids is None:
            continue
        for digest, record_id in zip(chunk.digests, chunk.record_ids, strict=False):
            if digest in wanted:
                record_ids[digest] = record_id
    return record_ids


def batch_submission_row(
    row: batch_model.BatchSubmission,
) -> BatchSubmissionOutput:
    return {
        "chunk_index": row.chunk_index,
        "digests": list(row.digests),
        "status": row.status,
        "record_ids": list(row.record_ids) if row.record_ids is not None else None,
        "submitted_at": row.submitted_at,
        "attempts": row.attempts,
        "last_error": row.last_error,
    }


def batch_verify_job_row(
    row: batch_model.BatchVerifyJob,
) -> BatchVerifyJobOutput:
    return {
        "job_id": row.job_id,
        "digests": list(row.digests),
        "status": row.status,
        "submitted_at": row.submitted_at,
        "results_url": row.results_url,
        "results_url_expires_at": row.results_url_expires_at,
        "attempts": row.attempts,
        "completed_at": row.completed_at,
        "last_error": row.last_error,
    }


def submit_status_by_digest(
    batch: batch_model.BatchFile, row: batch_model.BatchObject
) -> dict[str, batch_model.BatchSubmissionStatus | None]:
    statuses: dict[str, batch_model.BatchSubmissionStatus | None] = dict.fromkeys(
        row.digests
    )
    wanted = set(row.digests)
    for chunk in batch.submissions:
        for digest in chunk.digests:
            if digest in wanted:
                statuses[digest] = chunk.status
    return statuses


def verify_status_by_digest(
    batch: batch_model.BatchFile, row: batch_model.BatchObject
) -> dict[str, batch_model.BatchVerifyJobStatus | None]:
    statuses: dict[str, batch_model.BatchVerifyJobStatus | None] = dict.fromkeys(
        row.digests
    )
    wanted = set(row.digests)
    for job in batch.verify_jobs:
        for digest in job.digests:
            if digest in wanted:
                statuses[digest] = job.status
    return statuses


def verified_count_for_row(
    batch: batch_model.BatchFile, row: batch_model.BatchObject, state: OtsState | None
) -> int:
    if state is None:
        return 0
    return sum(1 for digest in set(row.digests) if state.verified(digest))


def verbose_digest_rows(
    batch: batch_model.BatchFile,
    row: batch_model.BatchObject,
    state: OtsState | None,
) -> list[BatchVerboseDigestOutput]:
    object_ids = object_record_ids_by_digest(batch, row, state)
    batch_ids = batch_record_ids_by_digest(batch, row)
    submit_statuses = submit_status_by_digest(batch, row)
    verify_statuses = verify_status_by_digest(batch, row)
    verified = (
        set()
        if state is None
        else {digest for digest in row.digests if state.verified(digest)}
    )
    rows: list[BatchVerboseDigestOutput] = []
    for digest in row.digests:
        rows.append(
            {
                "digest": digest,
                "submitted": (object_ids.get(digest) or batch_ids.get(digest))
                is not None,
                "verified": digest in verified,
                "record_id": object_ids.get(digest) or batch_ids.get(digest),
                "submit_status": submit_statuses[digest],
                "verify_status": verify_statuses[digest],
            }
        )
    return rows


def verify_job_covers(
    batch: batch_model.BatchFile, row: batch_model.BatchObject
) -> bool:
    wanted = set(row.digests)
    return any(
        job.status in batch_model.BATCH_VERIFY_ACTIVE_STATUSES
        and wanted.issubset(set(job.digests))
        for job in batch.verify_jobs
    )


def next_for_row(
    batch: batch_model.BatchFile,
    row: batch_model.BatchObject,
    *,
    digests_submitted: int,
) -> str:
    reason = row.last_error or row.skipped_reason
    if row.status in {"failed", "skipped"} or reason is not None:
        if reason == "transient_sp_error" and row.status == "submitted":
            return hints.batch("upgrade", batch.batch_id)
        return (
            reason_next(reason, batch=batch, object_id=row.object_id)
            or "Inspect batch status"
        )
    if batch_complete(batch):
        return f"Batch complete; {hints.batch('rm', batch.batch_id)} to clean up"
    if row.status == "prepared":
        return hints.batch("submit", batch.batch_id)
    if row.status == "submitted":
        if digests_submitted < len(row.digests):
            return hints.batch("retry", batch.batch_id)
        if verify_job_covers(batch, row):
            return f"{hints.batch('upgrade', batch.batch_id)} (later)"
        return hints.batch("upgrade", batch.batch_id, "--watch")
    if row.status == "upgraded":
        return "Object complete"
    return "Inspect batch status"


def batch_status_dict(
    staging_root: Path, batch: batch_model.BatchFile, *, verbose: bool = False
) -> BatchStatusOutput:
    objects: list[BatchStatusObjectOutput] = []
    for row in batch.objects:
        state = load_object_ots(staging_root, batch, row.object_id)
        phase = state.phase.name if state is not None else None
        batch_submitted = submitted_count_from_batch(row, batch)
        object_submitted = (
            submitted_count_from_object(batch, row, state) if state is not None else 0
        )
        digests_submitted = max(batch_submitted, object_submitted)
        object_data: BatchStatusObjectOutput = {
            "object_id": row.object_id,
            "phase": phase,
            "batch_status": row.status,
            "digests_total": len(row.digests),
            "digests_verified": verified_count_for_row(batch, row, state),
            "digests_submitted": digests_submitted,
            "last_error": row.last_error,
            "skipped_reason": row.skipped_reason,
            "next": next_for_row(batch, row, digests_submitted=digests_submitted),
            "batch_id": batch.batch_id,
        }
        if verbose:
            object_data["digests"] = verbose_digest_rows(batch, row, state)
        objects.append(object_data)
    return {
        "batch_id": batch.batch_id,
        "target": batch.target,
        "root_path": batch.root_path,
        "category_id": batch.category_id,
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
        "counts": batch_counts(batch),
        "objects": objects,
        "submissions": [batch_submission_row(row) for row in batch.submissions],
        "verify_jobs": [batch_verify_job_row(row) for row in batch.verify_jobs],
    }


def batch_object_event(
    staging_root: Path, batch: batch_model.BatchFile, row: batch_model.BatchObject
) -> BatchObjectEventOutput:
    state = load_object_ots(staging_root, batch, row.object_id)
    batch_submitted = submitted_count_from_batch(row, batch)
    object_submitted = (
        submitted_count_from_object(batch, row, state) if state is not None else 0
    )
    digests_submitted = max(batch_submitted, object_submitted)
    return {
        "event": event_for_row(row),
        "batch_id": batch.batch_id,
        "target": batch.target,
        "object_id": row.object_id,
        "status": row.status,
        "digests_total": len(row.digests),
        "digests_submitted": digests_submitted,
        "digests_verified": verified_count_for_row(batch, row, state),
        "last_error": row.last_error,
        "skipped_reason": row.skipped_reason,
        "next": next_for_row(batch, row, digests_submitted=digests_submitted),
    }


def batch_verified_digest_counts(
    staging_root: Path, batch: batch_model.BatchFile
) -> tuple[int, int]:
    verified = 0
    total = 0
    for row in batch.objects:
        if row.status == "skipped":
            continue
        total += len(row.digests)
        state = load_object_ots(staging_root, batch, row.object_id)
        verified += verified_count_for_row(batch, row, state)
    return verified, total


def batch_verify_job_status_counts(batch: batch_model.BatchFile) -> str:
    if not batch.verify_jobs:
        return "none"
    counts: dict[str, int] = dict.fromkeys(batch_model.BATCH_VERIFY_JOB_STATUSES, 0)
    for job in batch.verify_jobs:
        counts[job.status] += 1
    return " ".join(
        f"{count} {status.lower()}" for status, count in counts.items() if count
    )


def commit_object_event(
    *,
    batch: batch_model.BatchFile,
    object_id: str,
    event: CommitObjectEventName,
    reason: str | None = None,
    message: str | None = None,
) -> CommitObjectEventOutput:
    return {
        "event": event,
        "batch_id": batch.batch_id,
        "target": batch.target,
        "object_id": object_id,
        "reason": reason,
        "message": message,
    }


def commit_summary_event(
    batch: batch_model.BatchFile, counts: CommitCountsOutput
) -> CommitSummaryEventOutput:
    return {
        "event": "summary",
        "batch_id": batch.batch_id,
        "target": batch.target,
        "category_id": batch.category_id,
        "root_path": batch.root_path,
        "counts": counts,
    }


def remove_batch(
    staging_root: Path, batch_id: str, *, force: bool, runtime: Runtime
) -> None:
    """Delete the batch file and its lock file; refuses open rows unless `force`.

    Holds the batch lock, so a running driver blocks it (exit 4). The lock
    file goes too, while held: a command that was waiting on it then finds
    no batch file (ADR-0005).
    """
    with batch_state.batch_lock(staging_root, batch_id, runtime):
        batch = load_batch_or_usage(staging_root, batch_id)
        open_rows = [
            row for row in batch.objects if row.status in {"prepared", "submitted"}
        ]
        if open_rows and not force:
            raise CLIError(
                f"batch {batch_id} has non-terminal rows; use --force to delete "
                "the batch file only"
            )
        batch_state.batch_file_path(staging_root, batch_id).unlink()
        batch_state.batch_lock_path(staging_root, batch_id).unlink(missing_ok=True)
