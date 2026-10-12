"""stage timestamp batch, stage commit --batch, and the batch engine.

The engine (submit, retry, upgrade, watch over a batch file) still lives
here; it moves into the use cases next (docs/roadmap.md §5 item 2).
"""

import contextlib
import json
import logging
from pathlib import Path  # noqa: TC003 - Typer reads option annotations at runtime.
from typing import TYPE_CHECKING, Annotated, Literal, NotRequired, TypedDict, cast

import msgspec
import typer

from ocfl_cli import _appctx as appctx
from ocfl_cli import errors as errors
from ocfl_cli._common import open_root, output_json
from ocfl_cli.errors import (
    AttestationMismatchError,
    BatchFileMissingError,
    CLIError,
    ErrorContext,
    LockTimeoutError,
    MissingStagedMetadataError,
    PhaseError,
    ReorgDetectedError,
    SimpleProofBadRequest,
    SimpleProofError,
    SimpleProofTransient,
    StagingError,
    UsageError,
)
from ocfl_cli.model import batch as batch_model
from ocfl_cli.model.ots_state import OtsState, Submission, VerifiedProof
from ocfl_cli.model.phase import Phase
from ocfl_cli.ots.bitcoin import (
    PendingVerification,
    VerifiedAttestation,
    verify_attestation,
)
from ocfl_cli.ots.simpleproof import VERIFY_BATCH_CHUNK_SIZE
from ocfl_cli.stage._app import (
    require_bitcoin,
    require_config,
    require_simpleproof,
    resolve_root_path,
    stage_app,
    stage_store,
)
from ocfl_cli.stage.render import (
    render_section,
    short_job_id,
)
from ocfl_cli.store import batch as batch_state
from ocfl_cli.store.ots_state import (
    has_ots,
    load_ots,
    ots_cache_path,
    plan_path,
    save_ots,
    with_lock,
)
from ocfl_cli.store.staging import StagingStore
from ocfl_cli.workflow import commit as wf_commit
from ocfl_cli.workflow import common as wf_common
from ocfl_cli.workflow import content as wf_content
from ocfl_cli.workflow import inventory as wf_inventory
from ocfl_cli.workflow import watch
from ocfl_lib import BitcoinRpcError, DigestAlgorithm

if TYPE_CHECKING:
    from ocfl_cli.model.simpleproof import SimpleProofDownloadResult
    from ocfl_cli.ports import HashBlockStream


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# timestamp content / inventory.
# ---------------------------------------------------------------------------


_BATCH_STATUSES = ("prepared", "submitted", "upgraded", "failed", "skipped")
_BATCH_MISSING_NEXT = "Operate per-object via stage timestamp content/inventory"

type FixedBatchReason = Literal[
    "attestation_mismatch",
    "batch_file_missing",
    "committed",
    "content_not_upgraded",
    "lock_contended",
    "missing_metadata",
    "missing_record_ids",
    "no_new_digests",
    "noop",
    "regressed",
    "reorg_deeper_than_min_conf",
    "results_expired",
    "sp_partial_results",
    "submit_pending",
    "transient_rpc_error",
    "transient_sp_error",
    "verify_job_failed",
]
type PrefixedBatchReason = Literal["category_mismatch", "in_batch"]

_FIXED_BATCH_REASONS: tuple[FixedBatchReason, ...] = (
    "attestation_mismatch",
    "batch_file_missing",
    "committed",
    "content_not_upgraded",
    "lock_contended",
    "missing_metadata",
    "missing_record_ids",
    "no_new_digests",
    "noop",
    "regressed",
    "reorg_deeper_than_min_conf",
    "results_expired",
    "sp_partial_results",
    "submit_pending",
    "transient_rpc_error",
    "transient_sp_error",
    "verify_job_failed",
)


def _fixed_batch_reason(reason: FixedBatchReason) -> FixedBatchReason:
    return reason


def _in_batch_reason(batch_id: str) -> str:
    return f"in_batch={batch_id}"


def _category_mismatch_reason(category_id: int) -> str:
    return f"category_mismatch={category_id}"


def _split_prefixed_batch_reason(reason: str) -> tuple[PrefixedBatchReason, str] | None:
    match reason.split("=", maxsplit=1):
        case ["in_batch", batch_id]:
            return "in_batch", batch_id
        case ["category_mismatch", category_id]:
            return "category_mismatch", category_id
        case _:
            return None


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
    target: batch_model.BatchTarget
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
    target: batch_model.BatchTarget
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
    target: batch_model.BatchTarget
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
    target: batch_model.BatchTarget
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
    target: batch_model.BatchTarget
    category_id: int
    root_path: str
    counts: CommitCountsOutput


class BatchCreatedEventOutput(TypedDict):
    event: Literal["batch_created"]
    batch_id: str
    target: batch_model.BatchTarget
    category_id: int
    root_path: str
    created_at: str


class BatchResumedEventOutput(TypedDict):
    event: Literal["batch_resumed"]
    batch_id: str
    target: batch_model.BatchTarget
    category_id: int
    root_path: str


type BatchEventOutput = (
    BatchObjectEventOutput
    | BatchSummaryEventOutput
    | BatchCreatedEventOutput
    | BatchResumedEventOutput
)


def _batch_counts(batch: batch_model.BatchFile) -> BatchCountsOutput:
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


def _emit_batch_event(ctx: typer.Context, data: BatchEventOutput) -> None:
    if appctx.json_mode(ctx):
        typer.echo(json.dumps(data, separators=(",", ":")))


def _event_for_row(row: batch_model.BatchObject) -> BatchObjectEventName:
    if row.status == "prepared":
        return "object_prepared"
    if row.status == "submitted":
        return "object_submitted"
    if row.status == "upgraded":
        return "object_upgraded"
    if row.status == "skipped":
        return "object_skipped"
    return "object_failed"


def _batch_to_list_row(batch: batch_model.BatchFile) -> dict:
    counts = _batch_counts(batch)
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


def _load_batches_or_exit(staging_root: Path) -> list[batch_model.BatchFile]:
    rows: list[batch_model.BatchFile] = []
    for path in sorted(batch_state.batches_dir(staging_root).glob("*.json")):
        rows.append(batch_state.load_batch(staging_root, path.stem))
    rows.sort(key=lambda batch: batch.updated_at, reverse=True)
    return rows


def _load_batch_or_usage(
    ctx: typer.Context, staging_root: Path, batch_id: str
) -> batch_model.BatchFile:
    path = batch_state.batch_file_path(staging_root, batch_id)
    if not path.is_file():
        raise BatchFileMissingError(
            f"batch_file_missing: {batch_id}; {_BATCH_MISSING_NEXT}"
        )
    return batch_state.load_batch(staging_root, batch_id)


def _reason_next(reason: str | None, *, batch: batch_model.BatchFile) -> str | None:
    if reason is None:
        return None
    match _split_prefixed_batch_reason(reason):
        case ("in_batch", batch_id):
            return f"stage timestamp batch status {batch_id}"
        case ("category_mismatch", _category_id):
            return (
                "Start a separate batch for that category or reset/recreate the "
                "staging session"
            )
        case None:
            pass
    match reason:
        case "regressed":
            return "stage status OBJECT; reset or restart timestamp flow"
        case "missing_record_ids" | "transient_sp_error" | "submit_pending":
            return (
                f"stage timestamp batch {batch.target} --batch {batch.batch_id} --retry"
            )
        case "lock_contended":
            return f"Re-run --batch {batch.batch_id} after the other process exits"
        case "committed" | "no_new_digests" | "noop":
            return "None"
        case "content_not_upgraded":
            return "Upgrade content first"
        case "missing_metadata":
            return "Add staged metadata or rerun with --allow-missing-metadata"
        case "sp_partial_results":
            return "Re-run --upgrade later"
        case "results_expired" | "verify_job_failed" | "transient_rpc_error":
            return "Re-run --upgrade"
        case "reorg_deeper_than_min_conf":
            return "Retry after node catches up or investigate chain state"
        case "attestation_mismatch":
            return "Operator inspection required"
        case "batch_file_missing":
            return _BATCH_MISSING_NEXT
        case _:
            return "Inspect batch status"


def _object_staging_dir(staging_root: Path, *, root_path: str, object_id: str) -> Path:
    return StagingStore(staging_root).staging_path(root_path, object_id)


def _load_object_ots(staging_root: Path, batch: batch_model.BatchFile, object_id: str):
    staging_dir = _object_staging_dir(
        staging_root, root_path=batch.root_path, object_id=object_id
    )
    if not has_ots(staging_dir):
        return None
    try:
        return load_ots(staging_dir)
    except StagingError:
        return None


def _batch_complete(batch: batch_model.BatchFile) -> bool:
    active = [row for row in batch.objects if row.status not in {"skipped", "failed"}]
    return bool(active) and all(row.status == "upgraded" for row in active)


def _submitted_count_from_batch(
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


def _submitted_count_from_object(
    batch: batch_model.BatchFile, row: batch_model.BatchObject, state: OtsState
) -> int:
    wanted = set(row.digests)
    submitted: set[str] = set()
    if batch.target == "content":
        for submission in state.submissions:
            if submission.batch_id != batch.batch_id or submission.record_ids is None:
                continue
            for digest, record_id in zip(
                submission.digests, submission.record_ids, strict=False
            ):
                if digest in wanted and record_id is not None:
                    submitted.add(digest)
        return len(submitted)
    proof = state.inventory_proof
    if (
        proof is not None
        and proof.batch_id == batch.batch_id
        and proof.digest in wanted
        and proof.record_id is not None
    ):
        return 1
    return 0


def _object_record_ids_by_digest(
    batch: batch_model.BatchFile, row: batch_model.BatchObject, state: OtsState | None
) -> dict[str, str | None]:
    record_ids: dict[str, str | None] = dict.fromkeys(row.digests)
    if state is None:
        return record_ids
    wanted = set(row.digests)
    if batch.target == "content":
        for submission in state.submissions:
            if submission.batch_id != batch.batch_id or submission.record_ids is None:
                continue
            for digest, record_id in zip(
                submission.digests, submission.record_ids, strict=False
            ):
                if digest in wanted:
                    record_ids[digest] = record_id
        return record_ids
    proof = state.inventory_proof
    if (
        proof is not None
        and proof.batch_id == batch.batch_id
        and proof.digest in wanted
    ):
        record_ids[proof.digest] = proof.record_id
    return record_ids


def _batch_record_ids_by_digest(
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


def _batch_submission_row(
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


def _batch_verify_job_row(
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


def _submit_status_by_digest(
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


def _verify_status_by_digest(
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


def _verified_count_for_row(
    batch: batch_model.BatchFile, row: batch_model.BatchObject, state: OtsState | None
) -> int:
    if state is None:
        return 0
    wanted = set(row.digests)
    if batch.target == "content":
        return sum(
            1
            for digest in wanted
            if state.content_proofs.get(digest, None) is not None
            and state.content_proofs[digest].verified
        )
    proof = state.inventory_proof
    if proof is None or proof.digest not in wanted:
        return 0
    return int(proof.proof.verified)


def _verbose_digest_rows(
    batch: batch_model.BatchFile,
    row: batch_model.BatchObject,
    state: OtsState | None,
) -> list[BatchVerboseDigestOutput]:
    object_ids = _object_record_ids_by_digest(batch, row, state)
    batch_ids = _batch_record_ids_by_digest(batch, row)
    submit_statuses = _submit_status_by_digest(batch, row)
    verify_statuses = _verify_status_by_digest(batch, row)
    verified: set[str] = set()
    if state is not None:
        if batch.target == "content":
            verified = {
                digest
                for digest in row.digests
                if state.content_proofs.get(digest, VerifiedProof()).verified
            }
        elif state.inventory_proof is not None and state.inventory_proof.proof.verified:
            verified = {state.inventory_proof.digest}
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


def _verify_job_covers(
    batch: batch_model.BatchFile, row: batch_model.BatchObject
) -> bool:
    wanted = set(row.digests)
    return any(
        job.status in batch_model.BATCH_VERIFY_ACTIVE_STATUSES
        and wanted.issubset(set(job.digests))
        for job in batch.verify_jobs
    )


def _next_for_row(
    batch: batch_model.BatchFile,
    row: batch_model.BatchObject,
    *,
    digests_submitted: int,
) -> str:
    reason = row.last_error or row.skipped_reason
    if row.status in {"failed", "skipped"} or reason is not None:
        if reason == "transient_sp_error" and row.status == "submitted":
            return "Re-run --upgrade"
        return _reason_next(reason, batch=batch) or "Inspect batch status"
    if _batch_complete(batch):
        return f"Batch complete; batch rm {batch.batch_id} to clean up"
    if row.status == "prepared":
        return f"Run --batch {batch.batch_id} to submit"
    if row.status == "submitted":
        if digests_submitted < len(row.digests):
            return f"Run --batch {batch.batch_id} --retry"
        if _verify_job_covers(batch, row):
            return "Re-run --upgrade later"
        return f"Run --batch {batch.batch_id} --upgrade"
    if row.status == "upgraded":
        return "Object complete"
    return "Inspect batch status"


def _batch_status_dict(
    staging_root: Path, batch: batch_model.BatchFile, *, verbose: bool = False
) -> BatchStatusOutput:
    objects: list[BatchStatusObjectOutput] = []
    for row in batch.objects:
        state = _load_object_ots(staging_root, batch, row.object_id)
        phase = state.phase.name if state is not None else None
        batch_submitted = _submitted_count_from_batch(row, batch)
        object_submitted = (
            _submitted_count_from_object(batch, row, state) if state is not None else 0
        )
        digests_submitted = max(batch_submitted, object_submitted)
        object_data: BatchStatusObjectOutput = {
            "object_id": row.object_id,
            "phase": phase,
            "batch_status": row.status,
            "digests_total": len(row.digests),
            "digests_verified": _verified_count_for_row(batch, row, state),
            "digests_submitted": digests_submitted,
            "last_error": row.last_error,
            "skipped_reason": row.skipped_reason,
            "next": _next_for_row(batch, row, digests_submitted=digests_submitted),
            "batch_id": batch.batch_id,
        }
        if verbose:
            object_data["digests"] = _verbose_digest_rows(batch, row, state)
        objects.append(object_data)
    return {
        "batch_id": batch.batch_id,
        "target": batch.target,
        "root_path": batch.root_path,
        "category_id": batch.category_id,
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
        "counts": _batch_counts(batch),
        "objects": objects,
        "submissions": [_batch_submission_row(row) for row in batch.submissions],
        "verify_jobs": [_batch_verify_job_row(row) for row in batch.verify_jobs],
    }


def _batch_object_event(
    staging_root: Path, batch: batch_model.BatchFile, row: batch_model.BatchObject
) -> BatchObjectEventOutput:
    state = _load_object_ots(staging_root, batch, row.object_id)
    batch_submitted = _submitted_count_from_batch(row, batch)
    object_submitted = (
        _submitted_count_from_object(batch, row, state) if state is not None else 0
    )
    digests_submitted = max(batch_submitted, object_submitted)
    return {
        "event": _event_for_row(row),
        "batch_id": batch.batch_id,
        "target": batch.target,
        "object_id": row.object_id,
        "status": row.status,
        "digests_total": len(row.digests),
        "digests_submitted": digests_submitted,
        "digests_verified": _verified_count_for_row(batch, row, state),
        "last_error": row.last_error,
        "skipped_reason": row.skipped_reason,
        "next": _next_for_row(batch, row, digests_submitted=digests_submitted),
    }


def _emit_batch_object_event(
    ctx: typer.Context,
    *,
    staging_root: Path,
    batch: batch_model.BatchFile,
    row: batch_model.BatchObject,
) -> None:
    _emit_batch_event(ctx, _batch_object_event(staging_root, batch, row))


def _emit_batch_object_changes(
    ctx: typer.Context,
    *,
    staging_root: Path,
    before: batch_model.BatchFile,
    after: batch_model.BatchFile,
) -> None:
    previous = {row.object_id: row for row in before.objects}
    for row in after.objects:
        old = previous.get(row.object_id)
        if old is None:
            _emit_batch_object_event(
                ctx, staging_root=staging_root, batch=after, row=row
            )
            continue
        old_event = _batch_object_event(staging_root, before, old)
        new_event = _batch_object_event(staging_root, after, row)
        if old_event != new_event:
            _emit_batch_event(ctx, new_event)


def _emit_batch_summary(ctx: typer.Context, batch: batch_model.BatchFile) -> None:
    event: BatchSummaryEventOutput = {
        "event": "summary",
        "batch_id": batch.batch_id,
        "target": batch.target,
        "category_id": batch.category_id,
        "root_path": batch.root_path,
        "counts": _batch_counts(batch),
    }
    _emit_batch_event(ctx, event)


def _batch_verified_digest_counts(
    staging_root: Path, batch: batch_model.BatchFile
) -> tuple[int, int]:
    verified = 0
    total = 0
    for row in batch.objects:
        if row.status == "skipped":
            continue
        total += len(row.digests)
        state = _load_object_ots(staging_root, batch, row.object_id)
        verified += _verified_count_for_row(batch, row, state)
    return verified, total


def _batch_verify_job_status_counts(batch: batch_model.BatchFile) -> str:
    if not batch.verify_jobs:
        return "none"
    counts: dict[str, int] = dict.fromkeys(batch_model.BATCH_VERIFY_JOB_STATUSES, 0)
    for job in batch.verify_jobs:
        counts[job.status] += 1
    return " ".join(
        f"{count} {status.lower()}" for status, count in counts.items() if count
    )


def _batch_verify_job_transitions(
    before: batch_model.BatchFile, after: batch_model.BatchFile
) -> tuple[str, ...]:
    prior = {job.job_id: job for job in before.verify_jobs}
    transitions: list[str] = []
    for job in after.verify_jobs:
        old = prior.get(job.job_id)
        if old is None:
            transitions.append(f"job {short_job_id(job.job_id)}: new {job.status}")
        elif old.status != job.status:
            transitions.append(
                f"job {short_job_id(job.job_id)}: {old.status} -> {job.status}"
            )
    return tuple(transitions)


def _short_block_hash(block_hash: str) -> str:
    if len(block_hash) <= 12:
        return block_hash
    return block_hash[-12:]


def _emit_batch_watch_block(
    ctx: typer.Context,
    *,
    staging_root: Path,
    before: batch_model.BatchFile,
    after: batch_model.BatchFile,
    height: int,
    block_hash: str,
    reverified: bool,
) -> None:
    if appctx.json_mode(ctx):
        return
    counts = _batch_counts(after)
    verified, total = _batch_verified_digest_counts(staging_root, after)
    jobs = _batch_verify_job_status_counts(after)
    reverify_text = " reverified" if reverified else ""
    typer.echo(
        f"block {height} {_short_block_hash(block_hash)}: "
        f"submitted={counts['submitted']} "
        f"upgraded={counts['upgraded']} failed={counts['failed']} "
        f"verified={verified}/{total} jobs={jobs}{reverify_text}"
    )
    for transition in _batch_verify_job_transitions(before, after):
        typer.echo(f"  {transition}")


def _emit_batch_watch_waiting(ctx: typer.Context, batch: batch_model.BatchFile) -> None:
    if appctx.json_mode(ctx):
        return
    counts = _batch_counts(batch)
    typer.echo(
        f"batch submitted: {batch.batch_id} submitted={counts['submitted']} "
        f"upgraded={counts['upgraded']} failed={counts['failed']}; "
        "waiting for Bitcoin blocks"
    )


def _commit_object_event(
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


def _commit_summary_event(
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


def _emit_commit_event(
    ctx: typer.Context, data: CommitObjectEventOutput | CommitSummaryEventOutput
) -> None:
    if appctx.json_mode(ctx):
        typer.echo(json.dumps(data, separators=(",", ":")))
        return
    if data["event"] == "summary":
        summary = cast("CommitSummaryEventOutput", data)
        counts = summary["counts"]
        typer.echo(
            f"batch committed: {summary['batch_id']} committed={counts['committed']} "
            f"skipped={counts['skipped']} failed={counts['failed']} "
            f"total={counts['total']}"
        )
        return
    object_id = data["object_id"]
    if data["event"] == "object_committed":
        typer.echo(f"{object_id}: committed")
    elif data["event"] == "object_skipped":
        typer.echo(f"{object_id}: skipped ({data['reason']})")
    else:
        typer.echo(f"{object_id}: failed ({data['reason']}): {data['message']}")


def _commit_failure_reason(exc: Exception) -> str:
    if isinstance(exc, AttestationMismatchError):
        return "attestation_mismatch"
    if isinstance(exc, PhaseError):
        return "phase_error"
    if isinstance(exc, MissingStagedMetadataError):
        return "missing_metadata"
    if isinstance(exc, errors.StagingFileMissingError):
        return "staging_file_missing"
    if isinstance(exc, errors.StagingFileChangedError):
        return "staging_file_changed"
    if isinstance(exc, errors.CommitFailedError):
        return "commit_failed"
    if isinstance(exc, LockTimeoutError):
        return "lock_contended"
    raise TypeError(f"unsupported batch commit exception {type(exc).__name__}")


def commit_batch(ctx: typer.Context, batch_id: str) -> None:
    store = stage_store(ctx)
    staging_root = store._root
    root_path = resolve_root_path(ctx)
    with batch_state.with_batch_lock(staging_root, batch_id):
        batch = _load_batch_or_usage(ctx, staging_root, batch_id)
        if batch.target != "inventory":
            raise UsageError(
                f"commit batches must target inventory; got {batch.target}"
            )
        if batch.root_path != root_path:
            raise UsageError(
                f"batch {batch.batch_id} belongs to root {batch.root_path}; "
                f"current root is {root_path}"
            )
        snapshot = tuple(
            (row.object_id, row.status, row.skipped_reason) for row in batch.objects
        )

    sr, fs = open_root(appctx.root(ctx))
    counts: CommitCountsOutput = {
        "committed": 0,
        "skipped": 0,
        "failed": 0,
        "total": len(snapshot),
    }
    failures = 0
    for object_id, status, skipped_reason in snapshot:
        staging_dir = store.staging_path(batch.root_path, object_id)
        if not staging_dir.exists():
            counts["skipped"] += 1
            _emit_commit_event(
                ctx,
                _commit_object_event(
                    batch=batch,
                    object_id=object_id,
                    event="object_skipped",
                    reason="committed",
                ),
            )
            continue
        if status != "upgraded":
            counts["skipped"] += 1
            _emit_commit_event(
                ctx,
                _commit_object_event(
                    batch=batch,
                    object_id=object_id,
                    event="object_skipped",
                    reason=skipped_reason or status,
                ),
            )
            continue
        try:
            wf_commit.commit_ots(
                store=store,
                sr=sr,
                fs=fs,
                root_path=batch.root_path,
                object_id=object_id,
                staging_dir=staging_dir,
                allow_missing_metadata=False,
            )
        except (
            AttestationMismatchError,
            PhaseError,
            MissingStagedMetadataError,
            errors.StagingFileMissingError,
            errors.StagingFileChangedError,
            errors.CommitFailedError,
            LockTimeoutError,
        ) as e:
            counts["failed"] += 1
            failures += 1
            _emit_commit_event(
                ctx,
                _commit_object_event(
                    batch=batch,
                    object_id=object_id,
                    event="object_failed",
                    reason=_commit_failure_reason(e),
                    message=str(e),
                ),
            )
            continue
        counts["committed"] += 1
        _emit_commit_event(
            ctx,
            _commit_object_event(
                batch=batch,
                object_id=object_id,
                event="object_committed",
            ),
        )
    _emit_commit_event(ctx, _commit_summary_event(batch, counts))
    if failures:
        raise typer.Exit(1)


def _read_objects_file(path: Path) -> list[str]:
    object_ids: list[str] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            object_ids.append(stripped)
    return object_ids


def _replace_batch_object(
    batch: batch_model.BatchFile, row: batch_model.BatchObject
) -> batch_model.BatchFile:
    rows = [
        existing for existing in batch.objects if existing.object_id != row.object_id
    ]
    rows.append(row)
    return msgspec.structs.replace(
        batch, objects=tuple(rows), updated_at=batch_state.now_utc_iso_z()
    )


def _object_has_other_batch(
    state: OtsState, batch_id: str, target: batch_model.BatchTarget
) -> str | None:
    if target == "inventory":
        proof = state.inventory_proof
        if (
            proof is not None
            and proof.batch_id is not None
            and proof.batch_id != batch_id
            and proof.record_id is None
        ):
            return proof.batch_id
        return None
    for submission in state.submissions:
        if (
            submission.status in {"submitting", "submitted"}
            and submission.batch_id is not None
            and submission.batch_id != batch_id
        ):
            return submission.batch_id
    return None


def _batch_command_tuple(target: str, batch_id: str) -> tuple[str, ...]:
    return ("stage", "timestamp", "batch", target, "--batch", batch_id)


def _prepare_batch_object(
    *,
    target: batch_model.BatchTarget,
    cfg,
    store: StagingStore,
    sr,
    root_path: str,
    staging_root: Path,
    batch: batch_model.BatchFile,
    object_id: str,
    allow_missing_metadata: bool,
) -> batch_model.BatchObject:
    staging_dir = store.staging_path(root_path, object_id)
    if not store.exists(root_path, object_id) or not has_ots(staging_dir):
        return batch_model.BatchObject(
            object_id=object_id,
            status="skipped",
            skipped_reason=_fixed_batch_reason("committed"),
        )
    try:
        with with_lock(staging_dir, timeout=wf_common.LOCK_TIMEOUT_SECONDS):
            state = load_ots(staging_dir)
            if state.category_id != batch.category_id:
                return batch_model.BatchObject(
                    object_id=object_id,
                    status="skipped",
                    skipped_reason=_category_mismatch_reason(state.category_id),
                )
            if (
                other := _object_has_other_batch(state, batch.batch_id, target)
            ) is not None:
                return batch_model.BatchObject(
                    object_id=object_id,
                    status="skipped",
                    skipped_reason=_in_batch_reason(other),
                )
            if target == "content":
                prepared = wf_content.prepare_content_submission_locked(
                    cfg, store, sr, root_path, object_id, staging_dir
                )
                if not prepared.digests:
                    return batch_model.BatchObject(
                        object_id=object_id,
                        status="skipped",
                        skipped_reason=(
                            _fixed_batch_reason("noop")
                            if prepared.noop_advanced
                            else _fixed_batch_reason("no_new_digests")
                        ),
                    )
                state = load_ots(staging_dir)
                submissions = list(state.submissions)
                submissions[-1] = msgspec.structs.replace(
                    submissions[-1],
                    batch_id=batch.batch_id,
                    record_ids=tuple(None for _ in prepared.digests),
                )
                save_ots(
                    staging_dir,
                    msgspec.structs.replace(state, submissions=tuple(submissions)),
                )
                return batch_model.BatchObject(
                    object_id=object_id, status="prepared", digests=prepared.digests
                )

            prepared = wf_inventory.prepare_inventory_submission_locked(
                cfg,
                store,
                sr,
                root_path,
                object_id,
                staging_dir,
                allow_missing_metadata=allow_missing_metadata,
            )
            if prepared.digest is None:
                return batch_model.BatchObject(
                    object_id=object_id,
                    status="skipped",
                    skipped_reason=_fixed_batch_reason("noop"),
                )
            state = load_ots(staging_dir)
            if state.inventory_proof is None:
                return batch_model.BatchObject(
                    object_id=object_id,
                    status="failed",
                    last_error=_fixed_batch_reason("regressed"),
                )
            save_ots(
                staging_dir,
                msgspec.structs.replace(
                    state,
                    inventory_proof=msgspec.structs.replace(
                        state.inventory_proof,
                        batch_id=batch.batch_id,
                        record_id=None,
                    ),
                ),
            )
            if prepared.plan is not None:
                plan_path(staging_dir).write_bytes(prepared.plan.to_json())
            return batch_model.BatchObject(
                object_id=object_id, status="prepared", digests=(prepared.digest,)
            )
    except LockTimeoutError:
        return batch_model.BatchObject(
            object_id=object_id,
            status="skipped",
            skipped_reason=_fixed_batch_reason("lock_contended"),
        )
    except MissingStagedMetadataError:
        return batch_model.BatchObject(
            object_id=object_id,
            status="skipped",
            skipped_reason=_fixed_batch_reason("missing_metadata"),
        )
    except PhaseError:
        reason = (
            _fixed_batch_reason("content_not_upgraded")
            if target == "inventory"
            else _fixed_batch_reason("no_new_digests")
        )
        return batch_model.BatchObject(
            object_id=object_id, status="skipped", skipped_reason=reason
        )


def _ensure_submit_chunks(batch: batch_model.BatchFile) -> batch_model.BatchFile:
    if batch.submissions:
        return batch
    digests = sorted(
        {d for row in batch.objects if row.status == "prepared" for d in row.digests}
    )
    chunks = tuple(
        batch_model.BatchSubmission(
            chunk_index=i // wf_content.SUBMIT_CHUNK_SIZE,
            digests=tuple(digests[i : i + wf_content.SUBMIT_CHUNK_SIZE]),
            status="pending",
        )
        for i in range(0, len(digests), wf_content.SUBMIT_CHUNK_SIZE)
    )
    return msgspec.structs.replace(
        batch, submissions=chunks, updated_at=batch_state.now_utc_iso_z()
    )


def _row_by_object(batch: batch_model.BatchFile) -> dict[str, batch_model.BatchObject]:
    return {row.object_id: row for row in batch.objects}


def _chunk_object_ids(
    batch: batch_model.BatchFile, chunk: batch_model.BatchSubmission
) -> list[str]:
    chunk_digests = set(chunk.digests)
    return [
        row.object_id
        for row in batch.objects
        if row.status in {"prepared", "submitted"}
        and chunk_digests.intersection(row.digests)
    ]


def _mirror_chunk_to_object(
    *,
    target: batch_model.BatchTarget,
    store: StagingStore,
    root_path: str,
    staging_root: Path,
    batch: batch_model.BatchFile,
    object_id: str,
    ids_by_digest: dict[str, str],
) -> bool:
    staging_dir = store.staging_path(root_path, object_id)
    with with_lock(staging_dir, timeout=wf_common.LOCK_TIMEOUT_SECONDS):
        state = load_ots(staging_dir)
        if target == "content":
            changed: list[Submission] = []
            completed = False
            for submission in state.submissions:
                if submission.batch_id != batch.batch_id:
                    changed.append(submission)
                    continue
                record_ids = list(
                    submission.record_ids
                    if submission.record_ids is not None
                    else tuple(None for _ in submission.digests)
                )
                for i, digest in enumerate(submission.digests):
                    if digest in ids_by_digest:
                        record_ids[i] = ids_by_digest[digest]
                completed = all(record_id is not None for record_id in record_ids)
                changed.append(
                    msgspec.structs.replace(
                        submission,
                        record_ids=tuple(record_ids),
                        status="submitted" if completed else "submitting",
                    )
                )
            phase = Phase.CONTENT_PENDING if completed else state.phase
            save_ots(
                staging_dir,
                msgspec.structs.replace(state, submissions=tuple(changed), phase=phase),
            )
            return completed

        proof = state.inventory_proof
        if proof is None or proof.batch_id != batch.batch_id:
            return False
        record_id = ids_by_digest.get(proof.digest)
        if record_id is None:
            return proof.record_id is not None
        save_ots(
            staging_dir,
            msgspec.structs.replace(
                state,
                inventory_proof=msgspec.structs.replace(proof, record_id=record_id),
            ),
        )
        return True


def _submit_batch(
    *,
    target: batch_model.BatchTarget,
    cfg,
    store: StagingStore,
    staging_root: Path,
    batch: batch_model.BatchFile,
    sp_factory,
) -> batch_model.BatchFile:
    batch = _ensure_submit_chunks(batch)
    with batch_state.with_batch_lock(staging_root, batch.batch_id):
        batch_state.save_batch(staging_root, batch)
    sp = sp_factory(cfg)
    rows = _row_by_object(batch)
    submissions = list(batch.submissions)
    for index, chunk in enumerate(submissions):
        if chunk.status == "failed":
            continue
        if chunk.status == "submitted" and chunk.record_ids is not None:
            ids_by_digest = dict(zip(chunk.digests, chunk.record_ids, strict=True))
        else:
            chunk, rows, object_ids = _revalidate_submit_chunk(
                target=target,
                store=store,
                batch=batch,
                chunk=chunk,
                rows=rows,
            )
            if not object_ids:
                submissions[index] = msgspec.structs.replace(
                    chunk,
                    status="submitted",
                    record_ids=(),
                )
                batch = _save_batch_submit_progress(
                    staging_root, batch, rows, submissions
                )
                continue
            try:
                result = sp.submit_hashes(list(chunk.digests), batch.category_id)
            except SimpleProofError as e:
                submissions[index] = msgspec.structs.replace(
                    chunk,
                    status="failed",
                    attempts=chunk.attempts + 1,
                    last_error=str(e),
                )
                for object_id in object_ids:
                    rows[object_id] = msgspec.structs.replace(
                        rows[object_id],
                        last_error=_fixed_batch_reason("transient_sp_error"),
                    )
                return _save_batch_submit_progress(
                    staging_root, batch, rows, submissions
                )
            if len(result.record_ids) != len(chunk.digests):
                failed = msgspec.structs.replace(
                    chunk,
                    status="failed",
                    attempts=chunk.attempts + 1,
                    last_error=_fixed_batch_reason("missing_record_ids"),
                )
                submissions[index] = failed
                for object_id in object_ids:
                    rows[object_id] = msgspec.structs.replace(
                        rows[object_id],
                        last_error=_fixed_batch_reason("missing_record_ids"),
                    )
                continue
            ids_by_digest = dict(zip(chunk.digests, result.record_ids, strict=True))
            submissions[index] = msgspec.structs.replace(
                chunk,
                status="submitted",
                record_ids=result.record_ids,
                submitted_at=batch_state.now_utc_iso_z(),
                attempts=chunk.attempts + 1,
            )
            batch = msgspec.structs.replace(
                batch,
                objects=tuple(rows.values()),
                submissions=tuple(submissions),
                updated_at=batch_state.now_utc_iso_z(),
            )
            with batch_state.with_batch_lock(staging_root, batch.batch_id):
                batch_state.save_batch(staging_root, batch)

        for object_id in _chunk_object_ids(batch, chunk):
            if _mirror_chunk_to_object(
                target=target,
                store=store,
                root_path=batch.root_path,
                staging_root=staging_root,
                batch=batch,
                object_id=object_id,
                ids_by_digest=ids_by_digest,
            ):
                rows[object_id] = msgspec.structs.replace(
                    rows[object_id], status="submitted", last_error=None
                )
    return _save_batch_submit_progress(staging_root, batch, rows, submissions)


def _object_ids_for_digests(
    batch: batch_model.BatchFile, digests: tuple[str, ...]
) -> list[str]:
    wanted = set(digests)
    return [
        row.object_id
        for row in batch.objects
        if row.status in {"prepared", "submitted"} and wanted.intersection(row.digests)
    ]


def _collect_retry_record_ids(
    *,
    target: batch_model.BatchTarget,
    store: StagingStore,
    staging_root: Path,
    batch: batch_model.BatchFile,
    submissions: list[batch_model.BatchSubmission],
) -> tuple[dict[str, str], dict[str, batch_model.BatchObject]]:
    rows = _row_by_object(batch)
    ids_by_digest: dict[str, str] = {}
    mutable_indexes_by_digest: dict[str, list[int]] = {}
    for index, chunk in enumerate(submissions):
        if chunk.record_ids is None:
            continue
        for digest, record_id in zip(chunk.digests, chunk.record_ids, strict=False):
            if record_id is None:
                continue
            ids_by_digest[digest] = record_id
            if chunk.status != "failed":
                mutable_indexes_by_digest.setdefault(digest, []).append(index)
    for row in batch.objects:
        if row.status not in {"prepared", "submitted"}:
            continue
        staging_dir = store.staging_path(batch.root_path, row.object_id)
        with with_lock(staging_dir, timeout=wf_common.LOCK_TIMEOUT_SECONDS):
            state = load_ots(staging_dir)
            for digest, record_id in _object_record_ids_by_digest(
                batch, row, state
            ).items():
                if record_id is not None:
                    existing = ids_by_digest.get(digest)
                    if existing is not None and existing != record_id:
                        for index in mutable_indexes_by_digest.get(digest, ()):
                            chunk = submissions[index]
                            if chunk.record_ids is None:
                                continue
                            record_ids = tuple(
                                record_id if chunk_digest == digest else chunk_record_id
                                for chunk_digest, chunk_record_id in zip(
                                    chunk.digests,
                                    chunk.record_ids,
                                    strict=False,
                                )
                            )
                            submissions[index] = msgspec.structs.replace(
                                chunk, record_ids=record_ids
                            )
                        ids_by_digest[digest] = record_id
                    else:
                        ids_by_digest[digest] = record_id
    for row in batch.objects:
        if rows[row.object_id].status not in {"prepared", "submitted"}:
            continue
        row_ids = {
            digest: record_id
            for digest in row.digests
            if (record_id := ids_by_digest.get(digest)) is not None
        }
        if row_ids and _mirror_chunk_to_object(
            target=target,
            store=store,
            root_path=batch.root_path,
            staging_root=staging_root,
            batch=batch,
            object_id=row.object_id,
            ids_by_digest=row_ids,
        ):
            rows[row.object_id] = msgspec.structs.replace(
                row, status="submitted", last_error=None
            )
    return ids_by_digest, rows


def _append_retry_chunk(
    submissions: list[batch_model.BatchSubmission],
    *,
    digests: tuple[str, ...],
    status: batch_model.BatchSubmissionStatus,
    record_ids: tuple[str, ...] | None = None,
    attempts: int = 0,
    last_error: str | None = None,
) -> batch_model.BatchSubmission:
    next_index = (
        max((submission.chunk_index for submission in submissions), default=-1) + 1
    )
    chunk = batch_model.BatchSubmission(
        chunk_index=next_index,
        digests=digests,
        status=status,
        record_ids=record_ids,
        submitted_at=batch_state.now_utc_iso_z() if status == "submitted" else None,
        attempts=attempts,
        last_error=last_error,
    )
    submissions.append(chunk)
    return chunk


def _drive_batch_retry(
    ctx: typer.Context, *, target: batch_model.BatchTarget, batch_id: str | None
) -> None:
    if batch_id is None:
        raise UsageError("--retry requires --batch")
    cfg = require_config(ctx)
    require_simpleproof(ctx, cfg)
    store = stage_store(ctx)
    staging_root = store._root
    batch = _load_batch_or_usage(ctx, staging_root, batch_id)
    if batch.target != target:
        raise UsageError(
            f"batch {batch.batch_id} target is {batch.target!r}, not {target!r}"
        )
    with batch_state.with_driver_lease(
        staging_root,
        batch.batch_id,
        command=(*_batch_command_tuple(target, batch.batch_id), "--retry"),
    ):
        _emit_batch_resumed(ctx, batch)
        before_retry = batch
        batch = _retry_batch_submissions(
            target=target,
            cfg=cfg,
            store=store,
            staging_root=staging_root,
            batch=batch,
            sp_factory=appctx.sp_client_factory(ctx),
        )
        _emit_batch_object_changes(
            ctx, staging_root=staging_root, before=before_retry, after=batch
        )
    _emit_batch_summary(ctx, batch)
    if not appctx.json_mode(ctx):
        counts = _batch_counts(batch)
        typer.echo(
            f"batch retried: {batch.batch_id} prepared={counts['prepared']} "
            f"submitted={counts['submitted']} failed={counts['failed']}"
        )


def _missing_prepared_retry_digests(
    *,
    target: batch_model.BatchTarget,
    store: StagingStore,
    batch: batch_model.BatchFile,
    rows: dict[str, batch_model.BatchObject],
    ids_by_digest: dict[str, str],
    source_digests: set[str],
) -> tuple[str, ...]:
    missing: list[str] = []
    seen: set[str] = set(source_digests)
    for row in batch.objects:
        if rows[row.object_id].status != "prepared":
            continue
        staging_dir = store.staging_path(batch.root_path, row.object_id)
        with with_lock(staging_dir, timeout=wf_common.LOCK_TIMEOUT_SECONDS):
            state = load_ots(staging_dir)
            object_ids = _object_record_ids_by_digest(batch, row, state)
        for digest in row.digests:
            if digest in seen:
                continue
            if ids_by_digest.get(digest) is None and object_ids.get(digest) is None:
                missing.append(digest)
                seen.add(digest)
    return tuple(missing)


def _save_retry_pre_post(
    staging_root: Path,
    batch: batch_model.BatchFile,
    rows: dict[str, batch_model.BatchObject],
    submissions: list[batch_model.BatchSubmission],
) -> batch_model.BatchFile:
    batch = msgspec.structs.replace(
        batch,
        objects=tuple(rows.values()),
        submissions=tuple(submissions),
        updated_at=batch_state.now_utc_iso_z(),
    )
    with batch_state.with_batch_lock(staging_root, batch.batch_id):
        batch_state.save_batch(staging_root, batch)
    return batch


def _retry_batch_submissions(
    *,
    target: batch_model.BatchTarget,
    cfg,
    store: StagingStore,
    staging_root: Path,
    batch: batch_model.BatchFile,
    sp_factory,
) -> batch_model.BatchFile:
    batch = _ensure_submit_chunks(batch)
    submissions = list(batch.submissions)
    ids_by_digest, rows = _collect_retry_record_ids(
        target=target,
        store=store,
        staging_root=staging_root,
        batch=batch,
        submissions=submissions,
    )
    retry_sources = [
        (index, chunk)
        for index, chunk in enumerate(submissions)
        if chunk.status in {"failed", "pending"}
    ]
    source_digests = {
        digest for _index, chunk in retry_sources for digest in chunk.digests
    }
    for digest in _missing_prepared_retry_digests(
        target=target,
        store=store,
        batch=batch,
        rows=rows,
        ids_by_digest=ids_by_digest,
        source_digests=source_digests,
    ):
        retry_sources.append(
            (
                len(submissions),
                _append_retry_chunk(
                    submissions,
                    digests=(digest,),
                    status="pending",
                ),
            )
        )
        source_digests.add(digest)
    processed_digests: set[str] = set()
    sp = sp_factory(cfg)
    for source_index, source in retry_sources:
        source_digests = tuple(
            digest for digest in source.digests if digest not in processed_digests
        )
        if not source_digests:
            continue
        repaired = tuple(
            (digest, record_id)
            for digest in source_digests
            if (record_id := ids_by_digest.get(digest)) is not None
        )
        if repaired:
            if len(repaired) == len(source_digests) and source.status == "pending":
                repair_chunk = msgspec.structs.replace(
                    source,
                    status="submitted",
                    record_ids=tuple(record_id for _digest, record_id in repaired),
                    submitted_at=batch_state.now_utc_iso_z(),
                )
                submissions[source_index] = repair_chunk
            else:
                repair_chunk = _append_retry_chunk(
                    submissions,
                    digests=tuple(digest for digest, _record_id in repaired),
                    status="submitted",
                    record_ids=tuple(record_id for _digest, record_id in repaired),
                )
            for object_id in _object_ids_for_digests(batch, repair_chunk.digests):
                if _mirror_chunk_to_object(
                    target=target,
                    store=store,
                    root_path=batch.root_path,
                    staging_root=staging_root,
                    batch=batch,
                    object_id=object_id,
                    ids_by_digest=dict(repaired),
                ):
                    rows[object_id] = msgspec.structs.replace(
                        rows[object_id], status="submitted", last_error=None
                    )
        to_post = tuple(
            digest for digest in source_digests if ids_by_digest.get(digest) is None
        )
        processed_digests.update(source_digests)
        if not to_post:
            continue
        retry_chunk = (
            msgspec.structs.replace(source, digests=to_post)
            if source.status == "pending"
            else batch_model.BatchSubmission(
                chunk_index=max(
                    (submission.chunk_index for submission in submissions), default=-1
                )
                + 1,
                digests=to_post,
                status="pending",
            )
        )
        retry_chunk, rows, object_ids = _revalidate_submit_chunk(
            target=target,
            store=store,
            batch=msgspec.structs.replace(batch, submissions=tuple(submissions)),
            chunk=retry_chunk,
            rows=rows,
        )
        if not object_ids:
            if source.status == "pending":
                submissions[source_index] = msgspec.structs.replace(
                    retry_chunk, status="submitted", record_ids=()
                )
            continue
        retry_chunk = msgspec.structs.replace(
            retry_chunk, attempts=retry_chunk.attempts + 1
        )
        if source.status == "pending":
            submissions[source_index] = retry_chunk
            retry_index = source_index
        else:
            submissions.append(retry_chunk)
            retry_index = len(submissions) - 1
        batch = _save_retry_pre_post(staging_root, batch, rows, submissions)
        try:
            result = sp.submit_hashes(list(retry_chunk.digests), batch.category_id)
        except SimpleProofError as e:
            submissions[retry_index] = msgspec.structs.replace(
                retry_chunk,
                status="failed",
                last_error=str(e),
            )
            for object_id in object_ids:
                rows[object_id] = msgspec.structs.replace(
                    rows[object_id],
                    last_error=_fixed_batch_reason("transient_sp_error"),
                )
            return _save_batch_submit_progress(staging_root, batch, rows, submissions)
        if len(result.record_ids) != len(retry_chunk.digests):
            submissions[retry_index] = msgspec.structs.replace(
                retry_chunk,
                status="failed",
                last_error=_fixed_batch_reason("missing_record_ids"),
            )
            for object_id in object_ids:
                rows[object_id] = msgspec.structs.replace(
                    rows[object_id],
                    last_error=_fixed_batch_reason("missing_record_ids"),
                )
            continue
        retry_ids = dict(zip(retry_chunk.digests, result.record_ids, strict=True))
        ids_by_digest.update(retry_ids)
        retry_chunk = msgspec.structs.replace(
            retry_chunk,
            status="submitted",
            record_ids=result.record_ids,
            submitted_at=batch_state.now_utc_iso_z(),
        )
        submissions[retry_index] = retry_chunk
        for object_id in object_ids:
            if _mirror_chunk_to_object(
                target=target,
                store=store,
                root_path=batch.root_path,
                staging_root=staging_root,
                batch=batch,
                object_id=object_id,
                ids_by_digest=retry_ids,
            ):
                rows[object_id] = msgspec.structs.replace(
                    rows[object_id], status="submitted", last_error=None
                )
    return _save_batch_submit_progress(staging_root, batch, rows, submissions)


def _collect_recoverable_object_intents(
    store: StagingStore, batch: batch_model.BatchFile
) -> tuple[batch_model.BatchObject, ...]:
    known = {row.object_id for row in batch.objects}
    recovered: list[batch_model.BatchObject] = []
    for staging_dir in sorted(store._root.iterdir()):
        if not staging_dir.is_dir() or not has_ots(staging_dir):
            continue
        inventory_path = staging_dir / "inventory.json"
        if not inventory_path.is_file():
            continue
        try:
            raw_inventory = msgspec.json.decode(inventory_path.read_bytes())
        except OSError, msgspec.DecodeError:
            continue
        if not isinstance(raw_inventory, dict):
            continue
        object_id = raw_inventory.get("id")
        if not isinstance(object_id, str) or object_id in known:
            continue
        if store.staging_path(batch.root_path, object_id) != staging_dir:
            continue
        with with_lock(staging_dir, timeout=wf_common.LOCK_TIMEOUT_SECONDS):
            state = load_ots(staging_dir)
            row = _recover_content_intent(batch, object_id, state)
            if batch.target == "inventory":
                row = _recover_inventory_intent(batch, object_id, state)
        if row is not None:
            recovered.append(row)
            known.add(object_id)
    return tuple(recovered)


def _merge_recovered_object_intents(
    batch: batch_model.BatchFile, recovered: tuple[batch_model.BatchObject, ...]
) -> batch_model.BatchFile:
    if not recovered:
        return batch
    known = {row.object_id for row in batch.objects}
    merged = list(batch.objects)
    for row in recovered:
        if row.object_id not in known:
            merged.append(row)
            known.add(row.object_id)
    if len(merged) == len(batch.objects):
        return batch
    return msgspec.structs.replace(
        batch, objects=tuple(merged), updated_at=batch_state.now_utc_iso_z()
    )


def _recover_content_intent(
    batch: batch_model.BatchFile, object_id: str, state: OtsState
) -> batch_model.BatchObject | None:
    for submission in state.submissions:
        if submission.batch_id != batch.batch_id:
            continue
        submitted = submission.record_ids is not None and all(
            record_id is not None for record_id in submission.record_ids
        )
        return batch_model.BatchObject(
            object_id=object_id,
            status="submitted" if submitted else "prepared",
            digests=submission.digests,
        )
    return None


def _recover_inventory_intent(
    batch: batch_model.BatchFile, object_id: str, state: OtsState
) -> batch_model.BatchObject | None:
    proof = state.inventory_proof
    if proof is None or proof.batch_id != batch.batch_id:
        return None
    return batch_model.BatchObject(
        object_id=object_id,
        status="submitted" if proof.record_id is not None else "prepared",
        digests=(proof.digest,),
    )


def _save_batch_submit_progress(
    staging_root: Path,
    batch: batch_model.BatchFile,
    rows: dict[str, batch_model.BatchObject],
    submissions: list[batch_model.BatchSubmission],
) -> batch_model.BatchFile:
    batch = msgspec.structs.replace(
        batch,
        objects=tuple(rows.values()),
        submissions=tuple(submissions),
        updated_at=batch_state.now_utc_iso_z(),
    )
    with batch_state.with_batch_lock(staging_root, batch.batch_id):
        batch_state.save_batch(staging_root, batch)
    return batch


def _revalidate_submit_chunk(
    *,
    target: batch_model.BatchTarget,
    store: StagingStore,
    batch: batch_model.BatchFile,
    chunk: batch_model.BatchSubmission,
    rows: dict[str, batch_model.BatchObject],
) -> tuple[
    batch_model.BatchSubmission,
    dict[str, batch_model.BatchObject],
    list[str],
]:
    valid_digests = set(chunk.digests)
    valid_object_ids: list[str] = []
    for object_id in _chunk_object_ids(batch, chunk):
        staging_dir = store.staging_path(batch.root_path, object_id)
        with with_lock(staging_dir, timeout=wf_common.LOCK_TIMEOUT_SECONDS):
            state = load_ots(staging_dir)
            if state.category_id != batch.category_id:
                rows[object_id] = msgspec.structs.replace(
                    rows[object_id],
                    status="failed",
                    last_error=_fixed_batch_reason("regressed"),
                )
                valid_digests.difference_update(rows[object_id].digests)
                continue
            if target == "content":
                row_digests = _revalidate_content_submission(
                    state, batch.batch_id, rows[object_id]
                )
            else:
                row_digests = _revalidate_inventory_submission(
                    state, batch.batch_id, rows[object_id]
                )
            if row_digests is None:
                rows[object_id] = msgspec.structs.replace(
                    rows[object_id],
                    status="failed",
                    last_error=_fixed_batch_reason("regressed"),
                )
                valid_digests.difference_update(rows[object_id].digests)
                continue
            chunk_row_digests = set(rows[object_id].digests).intersection(chunk.digests)
            already_recorded = chunk_row_digests - row_digests
            valid_digests.difference_update(already_recorded)
            if chunk_row_digests.intersection(valid_digests):
                valid_object_ids.append(object_id)
    filtered = tuple(digest for digest in chunk.digests if digest in valid_digests)
    if filtered != chunk.digests:
        chunk = msgspec.structs.replace(chunk, digests=filtered)
    return chunk, rows, valid_object_ids


def _revalidate_content_submission(
    state: OtsState, batch_id: str, row: batch_model.BatchObject
) -> set[str] | None:
    for submission in state.submissions:
        if submission.batch_id != batch_id:
            continue
        if not set(row.digests).issubset(set(submission.digests)):
            return None
        record_ids = (
            submission.record_ids
            if submission.record_ids is not None
            else tuple(None for _ in submission.digests)
        )
        return {
            digest
            for digest, record_id in zip(submission.digests, record_ids, strict=True)
            if digest in row.digests and record_id is None
        }
    return None


def _revalidate_inventory_submission(
    state: OtsState, batch_id: str, row: batch_model.BatchObject
) -> set[str] | None:
    proof = state.inventory_proof
    if proof is None or proof.batch_id != batch_id or proof.digest not in row.digests:
        return None
    if proof.record_id is not None:
        return set()
    return {proof.digest}


def _content_digest_verified(state: OtsState, digest: str) -> bool:
    proof = state.content_proofs.get(digest)
    return proof is not None and proof.verified


def _inventory_digest_verified(state: OtsState, digest: str) -> bool:
    proof = state.inventory_proof
    return proof is not None and proof.digest == digest and proof.proof.verified


def _batch_digest_verified(
    *, target: batch_model.BatchTarget, state: OtsState, digest: str
) -> bool:
    if target == "content":
        return _content_digest_verified(state, digest)
    return _inventory_digest_verified(state, digest)


def _covered_verify_digests(batch: batch_model.BatchFile) -> set[str]:
    return {
        digest
        for job in batch.verify_jobs
        if job.status in batch_model.BATCH_VERIFY_ACTIVE_STATUSES
        or (
            job.status == "COMPLETED"
            and job.last_error != _fixed_batch_reason("sp_partial_results")
        )
        for digest in job.digests
    }


def _batch_unverified_submitted_digests(
    *,
    target: batch_model.BatchTarget,
    store: StagingStore,
    batch: batch_model.BatchFile,
) -> tuple[str, ...]:
    covered = _covered_verify_digests(batch)
    pending: list[str] = []
    seen: set[str] = set()
    for row in batch.objects:
        if row.status != "submitted":
            continue
        staging_dir = store.staging_path(batch.root_path, row.object_id)
        with with_lock(staging_dir, timeout=wf_common.LOCK_TIMEOUT_SECONDS):
            state = load_ots(staging_dir)
            for digest in row.digests:
                if digest in seen or digest in covered:
                    continue
                if not _batch_digest_verified(
                    target=target, state=state, digest=digest
                ):
                    pending.append(digest)
                    seen.add(digest)
    return tuple(pending)


def _save_batch_upgrade(
    staging_root: Path, batch: batch_model.BatchFile
) -> batch_model.BatchFile:
    batch = msgspec.structs.replace(batch, updated_at=batch_state.now_utc_iso_z())
    with batch_state.with_batch_lock(staging_root, batch.batch_id):
        batch_state.save_batch(staging_root, batch)
    return batch


def _submit_batch_verify_jobs(
    *,
    target: batch_model.BatchTarget,
    cfg,
    store: StagingStore,
    staging_root: Path,
    batch: batch_model.BatchFile,
    sp_factory,
) -> batch_model.BatchFile:
    sp = sp_factory(cfg)
    digests = _batch_unverified_submitted_digests(
        target=target, store=store, batch=batch
    )
    if not digests:
        return batch
    jobs = list(batch.verify_jobs)
    for i in range(0, len(digests), VERIFY_BATCH_CHUNK_SIZE):
        chunk = tuple(digests[i : i + VERIFY_BATCH_CHUNK_SIZE])
        try:
            job_id = sp.verify_batch(list(chunk), batch.category_id).job_id
        except SimpleProofTransient as e:
            failed = batch_model.BatchVerifyJob(
                job_id=f"failed-{len(jobs) + 1}",
                digests=chunk,
                status="FAILED",
                submitted_at=batch_state.now_utc_iso_z(),
                attempts=1,
                last_error=str(e),
            )
            jobs.append(failed)
            rows = tuple(
                msgspec.structs.replace(
                    row, last_error=_fixed_batch_reason("transient_sp_error")
                )
                if row.status == "submitted" and set(row.digests).intersection(chunk)
                else row
                for row in batch.objects
            )
            batch = msgspec.structs.replace(
                batch, objects=rows, verify_jobs=tuple(jobs)
            )
            return _save_batch_upgrade(staging_root, batch)
        jobs.append(
            batch_model.BatchVerifyJob(
                job_id=job_id,
                digests=chunk,
                status="IN_PROGRESS",
                submitted_at=batch_state.now_utc_iso_z(),
                attempts=1,
            )
        )
        batch = msgspec.structs.replace(batch, verify_jobs=tuple(jobs))
        batch = _save_batch_upgrade(staging_root, batch)
    return batch


def _poll_batch_verify_jobs(
    *, cfg, staging_root: Path, batch: batch_model.BatchFile, sp_factory
) -> batch_model.BatchFile:
    sp = sp_factory(cfg)
    jobs: list[batch_model.BatchVerifyJob] = []
    rows = list(batch.objects)
    changed = False
    for job in batch.verify_jobs:
        if job.status not in batch_model.BATCH_VERIFY_ACTIVE_STATUSES:
            jobs.append(job)
            continue
        try:
            poll = sp.poll_job(job.job_id)
        except SimpleProofTransient as e:
            jobs.append(
                msgspec.structs.replace(
                    job,
                    status="FAILED",
                    completed_at=batch_state.now_utc_iso_z(),
                    last_error=str(e),
                )
            )
            affected = set(job.digests)
            rows = [
                msgspec.structs.replace(
                    row, last_error=_fixed_batch_reason("transient_sp_error")
                )
                if row.status == "submitted" and affected.intersection(row.digests)
                else row
                for row in rows
            ]
            changed = True
            continue
        except SimpleProofError as e:
            jobs.append(
                msgspec.structs.replace(
                    job,
                    status="FAILED",
                    completed_at=batch_state.now_utc_iso_z(),
                    last_error=str(e),
                )
            )
            affected = set(job.digests)
            rows = [
                msgspec.structs.replace(
                    row, last_error=_fixed_batch_reason("verify_job_failed")
                )
                if row.status == "submitted" and affected.intersection(row.digests)
                else row
                for row in rows
            ]
            changed = True
            continue
        status = (
            poll.status
            if poll.status in batch_model.BATCH_VERIFY_JOB_STATUSES
            else "IN_PROGRESS"
        )
        updated = msgspec.structs.replace(
            job,
            status=status,
            results_url=poll.results_url or job.results_url,
            completed_at=(
                batch_state.now_utc_iso_z()
                if status in batch_model.BATCH_VERIFY_TERMINAL_STATUSES
                else None
            ),
            last_error=(
                _fixed_batch_reason("verify_job_failed")
                if status == "FAILED"
                else _fixed_batch_reason("results_expired")
                if status == "EXPIRED"
                else job.last_error
            ),
        )
        jobs.append(updated)
        changed = changed or updated != job
        if status == "FAILED":
            affected = set(job.digests)
            rows = [
                msgspec.structs.replace(
                    row, last_error=_fixed_batch_reason("verify_job_failed")
                )
                if row.status == "submitted" and affected.intersection(row.digests)
                else row
                for row in rows
            ]
        if status == "EXPIRED":
            affected = set(job.digests)
            rows = [
                msgspec.structs.replace(
                    row, last_error=_fixed_batch_reason("results_expired")
                )
                if row.status == "submitted" and affected.intersection(row.digests)
                else row
                for row in rows
            ]
    if not changed:
        return batch
    return _save_batch_upgrade(
        staging_root,
        msgspec.structs.replace(batch, objects=tuple(rows), verify_jobs=tuple(jobs)),
    )


def _content_row_upgraded(state: OtsState, row: batch_model.BatchObject) -> bool:
    return all(_content_digest_verified(state, digest) for digest in row.digests)


def _inventory_row_upgraded(state: OtsState, row: batch_model.BatchObject) -> bool:
    return all(_inventory_digest_verified(state, digest) for digest in row.digests)


def _ingest_batch_content_downloads(
    *,
    store: StagingStore,
    sr,
    batch: batch_model.BatchFile,
    row: batch_model.BatchObject,
    downloads: SimpleProofDownloadResult,
    btc,
    min_confirmations: int,
) -> tuple[batch_model.BatchObject, bool]:
    staging_dir = store.staging_path(batch.root_path, row.object_id)
    with with_lock(staging_dir, timeout=wf_common.LOCK_TIMEOUT_SECONDS):
        state = load_ots(staging_dir)
        inv = store.load_inventory(batch.root_path, row.object_id)
        alg = DigestAlgorithm.from_ocfl_name(inv.digest_algorithm)
        head_state = {d: list(ps) for d, ps in inv.versions[inv.head].state.items()}
        for digest in row.digests:
            ots_bytes = downloads.get(digest)
            if ots_bytes is None or _content_digest_verified(state, digest):
                continue
            state, inv = wf_content.verify_and_ingest_digest(
                digest,
                ots_bytes,
                state,
                inv,
                head_state,
                alg,
                btc=btc,
                store=store,
                root_path=batch.root_path,
                object_id=row.object_id,
                staging_dir=staging_dir,
                min_confirmations=min_confirmations,
            )
        ready, _unverified_pending, _orphans = wf_content.phase_advance_ready(
            state, store, sr, batch.root_path, row.object_id
        )
        if ready and state.phase is not Phase.CONTENT_UPGRADED:
            state = msgspec.structs.replace(state, phase=Phase.CONTENT_UPGRADED)
            save_ots(staging_dir, state)
        if _content_row_upgraded(state, row):
            return (
                msgspec.structs.replace(row, status="upgraded", last_error=None),
                True,
            )
    return row, False


def _ingest_batch_inventory_downloads(
    *,
    store: StagingStore,
    sr,
    batch: batch_model.BatchFile,
    row: batch_model.BatchObject,
    downloads: SimpleProofDownloadResult,
    btc,
    min_confirmations: int,
) -> tuple[batch_model.BatchObject, bool]:
    staging_dir = store.staging_path(batch.root_path, row.object_id)
    with with_lock(staging_dir, timeout=wf_common.LOCK_TIMEOUT_SECONDS):
        state = load_ots(staging_dir)
        proof = state.inventory_proof
        if proof is None or proof.batch_id != batch.batch_id:
            return (
                msgspec.structs.replace(
                    row, status="failed", last_error=_fixed_batch_reason("regressed")
                ),
                True,
            )
        if not _inventory_plan_matches_submission(
            store=store,
            sr=sr,
            batch=batch,
            row=row,
            staging_dir=staging_dir,
            state=state,
        ):
            return (
                msgspec.structs.replace(
                    row, status="failed", last_error=_fixed_batch_reason("regressed")
                ),
                True,
            )
        ots_bytes = downloads.get(proof.digest)
        if ots_bytes is None or proof.proof.verified:
            return row, False
        state = wf_inventory.verify_inventory_digest(
            state, ots_bytes, btc, staging_dir, min_confirmations
        )
        if _inventory_row_upgraded(state, row):
            return (
                msgspec.structs.replace(row, status="upgraded", last_error=None),
                True,
            )
    return row, False


def _inventory_plan_matches_submission(
    *,
    store: StagingStore,
    sr,
    batch: batch_model.BatchFile,
    row: batch_model.BatchObject,
    staging_dir: Path,
    state: OtsState,
) -> bool:
    proof = state.inventory_proof
    if proof is None or proof.digest not in row.digests:
        return False
    prior_plan = wf_commit.load_plan(staging_dir)
    if prior_plan is None:
        return False
    prior_inv = prior_plan.inventory.inventory
    prior_created = prior_inv.versions[prior_inv.head].created
    metadata = store.load_metadata(batch.root_path, row.object_id)
    plan, _content_source = wf_commit.build_inventory_plan(
        store,
        sr,
        batch.root_path,
        row.object_id,
        message=metadata.message,
        user=wf_commit.version_user_from_staged(metadata),
        created=prior_created,
    )
    if plan.inventory.digest != proof.digest:
        return False
    plan_path(staging_dir).write_bytes(plan.to_json())
    return True


def _ingest_batch_verify_jobs(
    *,
    target: batch_model.BatchTarget,
    cfg,
    store: StagingStore,
    sr,
    staging_root: Path,
    batch: batch_model.BatchFile,
    sp_factory,
    btc_factory,
) -> batch_model.BatchFile:
    if cfg.bitcoin is None:
        raise RuntimeError("bitcoin config missing")
    sp = sp_factory(cfg)
    btc = btc_factory(cfg)
    min_confirmations = cfg.bitcoin.confirmations
    rows = {row.object_id: row for row in batch.objects}
    jobs = list(batch.verify_jobs)
    changed = False
    for index, job in enumerate(jobs):
        if job.status != "COMPLETED" or job.results_url is None:
            continue
        try:
            downloads = sp.download_results(job.results_url, set(job.digests))
        except SimpleProofBadRequest as e:
            jobs[index] = msgspec.structs.replace(
                job, status="EXPIRED", last_error=str(e)
            )
            affected = set(job.digests)
            rows = {
                object_id: (
                    msgspec.structs.replace(
                        row, last_error=_fixed_batch_reason("results_expired")
                    )
                    if row.status == "submitted" and affected.intersection(row.digests)
                    else row
                )
                for object_id, row in rows.items()
            }
            changed = True
            continue
        except SimpleProofTransient as e:
            jobs[index] = msgspec.structs.replace(
                job, status="FAILED", last_error=str(e)
            )
            affected = set(job.digests)
            rows = {
                object_id: (
                    msgspec.structs.replace(
                        row, last_error=_fixed_batch_reason("transient_sp_error")
                    )
                    if row.status == "submitted" and affected.intersection(row.digests)
                    else row
                )
                for object_id, row in rows.items()
            }
            changed = True
            continue
        except AttestationMismatchError:
            jobs[index] = msgspec.structs.replace(
                job,
                status="COMPLETED",
                last_error=_fixed_batch_reason("attestation_mismatch"),
            )
            affected = set(job.digests)
            rows = {
                object_id: (
                    msgspec.structs.replace(
                        row,
                        status="failed",
                        last_error=_fixed_batch_reason("attestation_mismatch"),
                    )
                    if row.status == "submitted" and affected.intersection(row.digests)
                    else row
                )
                for object_id, row in rows.items()
            }
            changed = True
            continue
        missing = downloads.missing_digests
        for row in list(rows.values()):
            if row.status != "submitted" or not set(row.digests).intersection(
                downloads
            ):
                continue
            try:
                if target == "content":
                    new_row, row_changed = _ingest_batch_content_downloads(
                        store=store,
                        sr=sr,
                        batch=batch,
                        row=row,
                        downloads=downloads,
                        btc=btc,
                        min_confirmations=min_confirmations,
                    )
                else:
                    new_row, row_changed = _ingest_batch_inventory_downloads(
                        store=store,
                        sr=sr,
                        batch=batch,
                        row=row,
                        downloads=downloads,
                        btc=btc,
                        min_confirmations=min_confirmations,
                    )
            except AttestationMismatchError:
                new_row = msgspec.structs.replace(
                    row,
                    status="failed",
                    last_error=_fixed_batch_reason("attestation_mismatch"),
                )
                row_changed = True
            except ReorgDetectedError:
                new_row = msgspec.structs.replace(
                    row,
                    status="failed",
                    last_error=_fixed_batch_reason("reorg_deeper_than_min_conf"),
                )
                row_changed = True
            except BitcoinRpcError:
                new_row = msgspec.structs.replace(
                    row, last_error=_fixed_batch_reason("transient_rpc_error")
                )
                row_changed = True
            if row_changed:
                rows[row.object_id] = new_row
                changed = True
        if missing:
            jobs[index] = msgspec.structs.replace(
                jobs[index], last_error=_fixed_batch_reason("sp_partial_results")
            )
            rows = {
                object_id: (
                    msgspec.structs.replace(
                        row, last_error=_fixed_batch_reason("sp_partial_results")
                    )
                    if row.status == "submitted" and missing.intersection(row.digests)
                    else row
                )
                for object_id, row in rows.items()
            }
            changed = True
    if not changed:
        return batch
    return _save_batch_upgrade(
        staging_root,
        msgspec.structs.replace(
            batch,
            objects=tuple(rows.values()),
            verify_jobs=tuple(jobs),
        ),
    )


def _reconcile_upgraded_batch_rows(
    *,
    target: batch_model.BatchTarget,
    store: StagingStore,
    staging_root: Path,
    batch: batch_model.BatchFile,
) -> batch_model.BatchFile:
    rows: list[batch_model.BatchObject] = []
    changed = False
    for row in batch.objects:
        if row.status != "submitted":
            rows.append(row)
            continue
        staging_dir = store.staging_path(batch.root_path, row.object_id)
        with with_lock(staging_dir, timeout=wf_common.LOCK_TIMEOUT_SECONDS):
            state = load_ots(staging_dir)
            upgraded = (
                _content_row_upgraded(state, row)
                if target == "content"
                else _inventory_row_upgraded(state, row)
            )
        if upgraded:
            rows.append(
                msgspec.structs.replace(row, status="upgraded", last_error=None)
            )
            changed = True
        else:
            rows.append(row)
    if not changed:
        return batch
    return _save_batch_upgrade(
        staging_root, msgspec.structs.replace(batch, objects=tuple(rows))
    )


def _drive_batch_upgrade(
    ctx: typer.Context, *, target: batch_model.BatchTarget, batch_id: str | None
) -> None:
    if batch_id is None:
        raise UsageError("--upgrade requires --batch")
    cfg = require_config(ctx)
    require_simpleproof(ctx, cfg)
    require_bitcoin(ctx, cfg)
    store = stage_store(ctx)
    staging_root = store._root
    batch = _load_batch_or_usage(ctx, staging_root, batch_id)
    if batch.target != target:
        raise UsageError(
            f"batch {batch.batch_id} target is {batch.target!r}, not {target!r}"
        )
    sr, _fs = open_root(appctx.root(ctx))
    with batch_state.with_driver_lease(
        staging_root,
        batch.batch_id,
        command=(*_batch_command_tuple(target, batch.batch_id), "--upgrade"),
    ):
        _emit_batch_resumed(ctx, batch)
        before_upgrade = batch
        batch = _run_batch_upgrade_pass(
            target=target,
            cfg=cfg,
            store=store,
            sr=sr,
            staging_root=staging_root,
            batch=batch,
            sp_factory=appctx.sp_client_factory(ctx),
            btc_factory=appctx.btc_rpc_factory(ctx),
        )
        _emit_batch_object_changes(
            ctx, staging_root=staging_root, before=before_upgrade, after=batch
        )
    _emit_batch_summary(ctx, batch)
    if not appctx.json_mode(ctx):
        counts = _batch_counts(batch)
        typer.echo(
            f"batch upgraded: {batch.batch_id} submitted={counts['submitted']} "
            f"upgraded={counts['upgraded']} failed={counts['failed']}"
        )


def _run_batch_upgrade_pass(
    *,
    target: batch_model.BatchTarget,
    cfg,
    store: StagingStore,
    sr,
    staging_root: Path,
    batch: batch_model.BatchFile,
    sp_factory,
    btc_factory,
) -> batch_model.BatchFile:
    batch = _submit_batch_verify_jobs(
        target=target,
        cfg=cfg,
        store=store,
        staging_root=staging_root,
        batch=batch,
        sp_factory=sp_factory,
    )
    batch = _reconcile_upgraded_batch_rows(
        target=target,
        store=store,
        staging_root=staging_root,
        batch=batch,
    )
    batch = _poll_batch_verify_jobs(
        cfg=cfg,
        staging_root=staging_root,
        batch=batch,
        sp_factory=sp_factory,
    )
    batch = _ingest_batch_verify_jobs(
        target=target,
        cfg=cfg,
        store=store,
        sr=sr,
        staging_root=staging_root,
        batch=batch,
        sp_factory=sp_factory,
        btc_factory=btc_factory,
    )
    return _reconcile_upgraded_batch_rows(
        target=target,
        store=store,
        staging_root=staging_root,
        batch=batch,
    )


def _batch_has_submitted_subset(batch: batch_model.BatchFile) -> bool:
    return any(row.status == "submitted" for row in batch.objects)


def _batch_watch_complete(batch: batch_model.BatchFile) -> bool:
    return bool(batch.objects) and all(
        row.status in {"upgraded", "failed", "skipped"} for row in batch.objects
    )


def _batch_watch_successful(batch: batch_model.BatchFile) -> bool:
    return _batch_watch_complete(batch) and any(
        row.status == "upgraded" for row in batch.objects
    )


def _reverify_batch_content_row(
    *,
    btc,
    staging_dir: Path,
    state: OtsState,
    row: batch_model.BatchObject,
    min_confirmations: int,
) -> tuple[OtsState, set[str], set[str]]:
    proofs = dict(state.content_proofs)
    reset: set[str] = set()
    reorg: set[str] = set()
    for digest in row.digests:
        proof = proofs.get(digest)
        if proof is None or not proof.verified:
            continue
        cache = ots_cache_path(staging_dir, digest)
        if not cache.is_file():
            continue
        try:
            result = verify_attestation(
                rpc=btc,
                ots_bytes=cache.read_bytes(),
                expected_digest=bytes.fromhex(digest),
                min_confirmations=min_confirmations,
            )
        except AttestationMismatchError:
            proofs[digest] = VerifiedProof(ots_path=proof.ots_path, verified=False)
            reset.add(digest)
            continue
        except ReorgDetectedError:
            proofs[digest] = VerifiedProof(ots_path=proof.ots_path, verified=False)
            reorg.add(digest)
            continue
        match result:
            case VerifiedAttestation(block_height=h, confirmations=c):
                proofs[digest] = VerifiedProof(
                    ots_path=proof.ots_path,
                    verified=True,
                    block_height=h,
                    block_hash=result.block_hash,
                    confirmations_at_verify=c,
                    verified_at=proof.verified_at,
                )
            case PendingVerification():
                proofs[digest] = VerifiedProof(ots_path=proof.ots_path, verified=False)
                reset.add(digest)
    if proofs == state.content_proofs:
        return state, reset, reorg
    return msgspec.structs.replace(state, content_proofs=proofs), reset, reorg


def _reverify_batch_inventory_row(
    *,
    btc,
    staging_dir: Path,
    state: OtsState,
    row: batch_model.BatchObject,
    min_confirmations: int,
) -> tuple[OtsState, set[str], set[str]]:
    inv = state.inventory_proof
    if inv is None or inv.digest not in row.digests or not inv.proof.verified:
        return state, set(), set()
    cache = ots_cache_path(staging_dir, inv.digest)
    if not cache.is_file():
        return state, set(), set()
    try:
        result = verify_attestation(
            rpc=btc,
            ots_bytes=cache.read_bytes(),
            expected_digest=bytes.fromhex(inv.digest),
            min_confirmations=min_confirmations,
        )
    except AttestationMismatchError:
        proof = VerifiedProof(ots_path=inv.proof.ots_path, verified=False)
        return (
            msgspec.structs.replace(
                state, inventory_proof=msgspec.structs.replace(inv, proof=proof)
            ),
            {inv.digest},
            set(),
        )
    except ReorgDetectedError:
        proof = VerifiedProof(ots_path=inv.proof.ots_path, verified=False)
        return (
            msgspec.structs.replace(
                state, inventory_proof=msgspec.structs.replace(inv, proof=proof)
            ),
            set(),
            {inv.digest},
        )
    match result:
        case VerifiedAttestation(block_height=h, confirmations=c):
            proof = VerifiedProof(
                ots_path=inv.proof.ots_path,
                verified=True,
                block_height=h,
                block_hash=result.block_hash,
                confirmations_at_verify=c,
                verified_at=inv.proof.verified_at,
            )
            return (
                msgspec.structs.replace(
                    state, inventory_proof=msgspec.structs.replace(inv, proof=proof)
                ),
                set(),
                set(),
            )
        case PendingVerification():
            proof = VerifiedProof(ots_path=inv.proof.ots_path, verified=False)
            return (
                msgspec.structs.replace(
                    state, inventory_proof=msgspec.structs.replace(inv, proof=proof)
                ),
                {inv.digest},
                set(),
            )


def _reset_batch_row_phase(
    target: batch_model.BatchTarget, state: OtsState
) -> OtsState:
    phase = Phase.CONTENT_PENDING if target == "content" else Phase.INVENTORY_PENDING
    return msgspec.structs.replace(state, phase=phase)


def _reverify_batch_watch_rows(
    *,
    target: batch_model.BatchTarget,
    cfg,
    store: StagingStore,
    staging_root: Path,
    batch: batch_model.BatchFile,
    btc,
) -> batch_model.BatchFile:
    if cfg.bitcoin is None:
        raise RuntimeError("bitcoin config missing")
    reset: set[str] = set()
    rows: list[batch_model.BatchObject] = []
    changed_rows = False
    for row in batch.objects:
        if row.status not in {"submitted", "upgraded"}:
            rows.append(row)
            continue
        staging_dir = store.staging_path(batch.root_path, row.object_id)
        with with_lock(staging_dir, timeout=wf_common.LOCK_TIMEOUT_SECONDS):
            state = load_ots(staging_dir)
            if target == "content":
                state, row_reset, row_reorg = _reverify_batch_content_row(
                    btc=btc,
                    staging_dir=staging_dir,
                    state=state,
                    row=row,
                    min_confirmations=cfg.bitcoin.confirmations,
                )
            else:
                state, row_reset, row_reorg = _reverify_batch_inventory_row(
                    btc=btc,
                    staging_dir=staging_dir,
                    state=state,
                    row=row,
                    min_confirmations=cfg.bitcoin.confirmations,
                )
            if row_reorg:
                state = _reset_batch_row_phase(target, state)
                row = msgspec.structs.replace(
                    row,
                    status="failed",
                    last_error=_fixed_batch_reason("reorg_deeper_than_min_conf"),
                )
                changed_rows = True
            if row_reset:
                reset.update(row_reset)
                state = _reset_batch_row_phase(target, state)
                if row.status == "upgraded":
                    row = msgspec.structs.replace(
                        row, status="submitted", last_error=None
                    )
                    changed_rows = True
            save_ots(staging_dir, state)
        rows.append(row)
    if not reset:
        if not changed_rows:
            return batch
        return _save_batch_upgrade(
            staging_root, msgspec.structs.replace(batch, objects=tuple(rows))
        )
    kept_jobs = tuple(job for job in batch.verify_jobs if reset.isdisjoint(job.digests))
    return _save_batch_upgrade(
        staging_root,
        msgspec.structs.replace(batch, objects=tuple(rows), verify_jobs=kept_jobs),
    )


def _drive_batch_watch(
    ctx: typer.Context,
    *,
    target: batch_model.BatchTarget,
    object_ids: tuple[str, ...],
    objects_file: Path | None,
    batch_id: str | None,
    category: int | None,
    allow_missing_metadata: bool,
) -> None:
    cfg = require_config(ctx)
    require_simpleproof(ctx, cfg)
    require_bitcoin(ctx, cfg)
    sp_cfg = cfg.simpleproof
    if sp_cfg is None:
        raise RuntimeError("simpleproof config missing")
    if cfg.bitcoin is None:
        raise RuntimeError("bitcoin config missing")
    store = stage_store(ctx)
    staging_root = store._root
    root_path = resolve_root_path(ctx)
    sr, _fs = open_root(appctx.root(ctx))
    selected = list(object_ids)
    if objects_file is not None:
        selected.extend(_read_objects_file(objects_file))
    if batch_id is not None and (selected or category is not None):
        raise UsageError(
            "--batch is mutually exclusive with --object, --objects-file, "
            "and --category"
        )
    if batch_id is None and not selected:
        raise UsageError("new batch requires --object or --objects-file")

    if batch_id is None:
        now = batch_state.now_utc_iso_z()
        batch = batch_model.BatchFile(
            batch_id=batch_state.new_batch_id(),
            target=target,
            root_path=root_path,
            category_id=category if category is not None else sp_cfg.proof_category_id,
            created_at=now,
            updated_at=now,
        )
        created = True
    else:
        batch = _load_batch_or_usage(ctx, staging_root, batch_id)
        if batch.target != target:
            raise UsageError(
                f"batch {batch.batch_id} target is {batch.target!r}, not {target!r}"
            )
        created = False

    stream = None
    exit_code = 1

    def run_watch_upgrade_pass(
        batch: batch_model.BatchFile, *, reload_batch: bool = False
    ) -> tuple[batch_model.BatchFile, bool]:
        nonlocal exit_code
        if reload_batch:
            batch = batch_state.load_batch(staging_root, batch.batch_id)
        before_upgrade = batch
        batch = _run_batch_upgrade_pass(
            target=target,
            cfg=cfg,
            store=store,
            sr=sr,
            staging_root=staging_root,
            batch=batch,
            sp_factory=appctx.sp_client_factory(ctx),
            btc_factory=appctx.btc_rpc_factory(ctx),
        )
        _emit_batch_object_changes(
            ctx, staging_root=staging_root, before=before_upgrade, after=batch
        )
        if _batch_has_submitted_subset(batch):
            return batch, False
        exit_code = 0 if _batch_watch_successful(batch) else 1
        if exit_code:
            raise typer.Exit(exit_code)
        return batch, True

    try:
        with batch_state.with_driver_lease(
            staging_root,
            batch.batch_id,
            command=(*_batch_command_tuple(target, batch.batch_id), "--watch"),
        ):
            if created:
                with batch_state.with_batch_lock(staging_root, batch.batch_id):
                    batch_state.save_batch(staging_root, batch)
                _emit_batch_created(ctx, batch)
                for object_id in dict.fromkeys(selected):
                    row = _prepare_batch_object(
                        target=target,
                        cfg=cfg,
                        store=store,
                        sr=sr,
                        root_path=root_path,
                        staging_root=staging_root,
                        batch=batch,
                        object_id=object_id,
                        allow_missing_metadata=allow_missing_metadata,
                    )
                    batch = _replace_batch_object(batch, row)
                    with batch_state.with_batch_lock(staging_root, batch.batch_id):
                        batch_state.save_batch(staging_root, batch)
                    _emit_batch_object_event(
                        ctx, staging_root=staging_root, batch=batch, row=row
                    )
            else:
                _emit_batch_resumed(ctx, batch)
            recovered = _collect_recoverable_object_intents(store, batch)
            with batch_state.with_batch_lock(staging_root, batch.batch_id):
                before_recovered = batch
                batch = batch_state.load_batch(staging_root, batch.batch_id)
                batch = _merge_recovered_object_intents(batch, recovered)
                batch_state.save_batch(staging_root, batch)
            _emit_batch_object_changes(
                ctx, staging_root=staging_root, before=before_recovered, after=batch
            )
            before_submit = batch
            batch = _submit_batch(
                target=target,
                cfg=cfg,
                store=store,
                staging_root=staging_root,
                batch=batch,
                sp_factory=appctx.sp_client_factory(ctx),
            )
            _emit_batch_object_changes(
                ctx, staging_root=staging_root, before=before_submit, after=batch
            )
            batch, done = run_watch_upgrade_pass(batch)
            if done:
                return

            _emit_batch_watch_waiting(ctx, batch)
            btc = appctx.btc_rpc_factory(ctx)(cfg)
            stream = watch.block_stream_for(cfg)
            stream_cm = cast(
                "contextlib.AbstractContextManager[HashBlockStream]",
                stream
                if hasattr(stream, "__enter__")
                else contextlib.nullcontext(stream),
            )
            last_height: int | None = None
            last_tip_hash: str | None = None
            with stream_cm as zmq_sub:
                while True:
                    block = zmq_sub.wait_for_block(timeout_ms=5000)
                    if block is None:
                        batch, done = run_watch_upgrade_pass(batch, reload_batch=True)
                        if done:
                            return
                        continue
                    watch.sleep_after_block()
                    info = btc.getblockchaininfo()
                    _height_jumped = (
                        last_height is not None and info.height - last_height > 1
                    )
                    _tip_changed_at_stable = (
                        last_height is not None
                        and info.height == last_height
                        and last_tip_hash is not None
                        and last_tip_hash != info.best_block_hash
                    )
                    last_height = info.height
                    last_tip_hash = info.best_block_hash
                    batch = batch_state.load_batch(staging_root, batch.batch_id)
                    before_reverify = batch
                    if _height_jumped or _tip_changed_at_stable:
                        batch = _reverify_batch_watch_rows(
                            target=target,
                            cfg=cfg,
                            store=store,
                            staging_root=staging_root,
                            batch=batch,
                            btc=btc,
                        )
                        _emit_batch_object_changes(
                            ctx,
                            staging_root=staging_root,
                            before=before_reverify,
                            after=batch,
                        )
                    before_block_upgrade = batch
                    batch, done = run_watch_upgrade_pass(batch)
                    _emit_batch_watch_block(
                        ctx,
                        staging_root=staging_root,
                        before=before_block_upgrade,
                        after=batch,
                        height=info.height,
                        block_hash=info.best_block_hash,
                        reverified=_height_jumped or _tip_changed_at_stable,
                    )
                    if done:
                        return
    except KeyboardInterrupt:
        exit_code = 130
    except (BitcoinRpcError, ReorgDetectedError) as e:
        raise ErrorContext(e, prefix="halt: ") from e
    finally:
        if appctx.json_mode(ctx):
            _emit_batch_summary(ctx, batch)
        elif batch_id is not None:
            counts = _batch_counts(batch)
            typer.echo(
                f"batch watched: {batch.batch_id} submitted={counts['submitted']} "
                f"upgraded={counts['upgraded']} failed={counts['failed']}"
            )
    if exit_code:
        raise typer.Exit(exit_code)


def _emit_batch_created(ctx: typer.Context, batch: batch_model.BatchFile) -> None:
    data: BatchCreatedEventOutput = {
        "event": "batch_created",
        "batch_id": batch.batch_id,
        "target": batch.target,
        "category_id": batch.category_id,
        "root_path": batch.root_path,
        "created_at": batch.created_at,
    }
    if appctx.json_mode(ctx):
        _emit_batch_event(ctx, data)
    else:
        typer.echo(
            f"batch created: {batch.batch_id} target={batch.target} "
            f"category={batch.category_id}"
        )


def _emit_batch_resumed(ctx: typer.Context, batch: batch_model.BatchFile) -> None:
    if appctx.json_mode(ctx):
        data: BatchResumedEventOutput = {
            "event": "batch_resumed",
            "batch_id": batch.batch_id,
            "target": batch.target,
            "category_id": batch.category_id,
            "root_path": batch.root_path,
        }
        _emit_batch_event(
            ctx,
            data,
        )
    else:
        typer.echo(f"batch resumed: {batch.batch_id} target={batch.target}")


def _drive_batch_submit(
    ctx: typer.Context,
    *,
    target: batch_model.BatchTarget,
    object_ids: tuple[str, ...],
    objects_file: Path | None,
    batch_id: str | None,
    category: int | None,
    allow_missing_metadata: bool,
) -> None:
    cfg = require_config(ctx)
    require_simpleproof(ctx, cfg)
    sp_cfg = cfg.simpleproof
    if sp_cfg is None:
        raise RuntimeError("simpleproof config missing")
    store = stage_store(ctx)
    staging_root = store._root
    root_path = resolve_root_path(ctx)
    sr, _fs = open_root(appctx.root(ctx))

    selected = list(object_ids)
    if objects_file is not None:
        selected.extend(_read_objects_file(objects_file))
    if batch_id is not None and (selected or category is not None):
        raise UsageError(
            "--batch is mutually exclusive with --object, --objects-file, "
            "and --category"
        )
    if batch_id is None and not selected:
        raise UsageError("new batch requires --object or --objects-file")

    if batch_id is None:
        now = batch_state.now_utc_iso_z()
        batch = batch_model.BatchFile(
            batch_id=batch_state.new_batch_id(),
            target=target,
            root_path=root_path,
            category_id=category if category is not None else sp_cfg.proof_category_id,
            created_at=now,
            updated_at=now,
        )
        created = True
    else:
        batch = _load_batch_or_usage(ctx, staging_root, batch_id)
        if batch.target != target:
            raise UsageError(
                f"batch {batch.batch_id} target is {batch.target!r}, not {target!r}"
            )
        created = False

    with batch_state.with_driver_lease(
        staging_root,
        batch.batch_id,
        command=_batch_command_tuple(target, batch.batch_id),
    ):
        if created:
            with batch_state.with_batch_lock(staging_root, batch.batch_id):
                batch_state.save_batch(staging_root, batch)
            _emit_batch_created(ctx, batch)
            for object_id in dict.fromkeys(selected):
                row = _prepare_batch_object(
                    target=target,
                    cfg=cfg,
                    store=store,
                    sr=sr,
                    root_path=root_path,
                    staging_root=staging_root,
                    batch=batch,
                    object_id=object_id,
                    allow_missing_metadata=allow_missing_metadata,
                )
                batch = _replace_batch_object(batch, row)
                with batch_state.with_batch_lock(staging_root, batch.batch_id):
                    batch_state.save_batch(staging_root, batch)
                _emit_batch_object_event(
                    ctx, staging_root=staging_root, batch=batch, row=row
                )
        else:
            _emit_batch_resumed(ctx, batch)
        recovered = _collect_recoverable_object_intents(store, batch)
        with batch_state.with_batch_lock(staging_root, batch.batch_id):
            before_recovered = batch
            batch = batch_state.load_batch(staging_root, batch.batch_id)
            batch = _merge_recovered_object_intents(batch, recovered)
            batch_state.save_batch(staging_root, batch)
        _emit_batch_object_changes(
            ctx, staging_root=staging_root, before=before_recovered, after=batch
        )
        before_submit = batch
        batch = _submit_batch(
            target=target,
            cfg=cfg,
            store=store,
            staging_root=staging_root,
            batch=batch,
            sp_factory=appctx.sp_client_factory(ctx),
        )
        _emit_batch_object_changes(
            ctx, staging_root=staging_root, before=before_submit, after=batch
        )
    _emit_batch_summary(ctx, batch)
    if not appctx.json_mode(ctx):
        counts = _batch_counts(batch)
        typer.echo(
            f"batch submitted: {batch.batch_id} prepared={counts['prepared']} "
            f"submitted={counts['submitted']} skipped={counts['skipped']} "
            f"failed={counts['failed']}"
        )


def timestamp_batch_content(
    ctx: typer.Context,
    object_ids: Annotated[
        list[str] | None,
        typer.Option("--object", help="Object ID to include in a new batch."),
    ] = None,
    objects_file: Annotated[
        Path | None,
        typer.Option("--objects-file", help="File containing object IDs."),
    ] = None,
    batch_id: Annotated[
        str | None,
        typer.Option("--batch", help="Resume an existing batch."),
    ] = None,
    category: Annotated[
        int | None,
        typer.Option("--category", help="SimpleProof category for a new batch."),
    ] = None,
    upgrade: Annotated[
        bool,
        typer.Option("--upgrade", help="Poll SP verify jobs and ingest proofs."),
    ] = False,
    retry: Annotated[
        bool,
        typer.Option("--retry", help="Repair and resubmit failed batch chunks."),
    ] = False,
    watch_flag: Annotated[
        bool,
        typer.Option(
            "--watch",
            help="ZMQ-driven loop; runs a batch upgrade pass per Bitcoin block.",
        ),
    ] = False,
) -> None:
    """Create or resume a content timestamping batch."""
    if retry and (upgrade or watch_flag):
        raise UsageError("--retry is mutually exclusive with --upgrade and --watch")
    if upgrade and watch_flag:
        raise UsageError("--upgrade is mutually exclusive with --watch")
    if retry:
        if object_ids or objects_file is not None or category is not None:
            raise UsageError("--retry is only supported with --batch")
        _drive_batch_retry(ctx, target="content", batch_id=batch_id)
        return
    if watch_flag:
        _drive_batch_watch(
            ctx,
            target="content",
            object_ids=tuple(object_ids or ()),
            objects_file=objects_file,
            batch_id=batch_id,
            category=category,
            allow_missing_metadata=False,
        )
        return
    if upgrade:
        if object_ids or objects_file is not None or category is not None:
            raise UsageError("--upgrade is only supported with --batch")
        _drive_batch_upgrade(ctx, target="content", batch_id=batch_id)
        return
    _drive_batch_submit(
        ctx,
        target="content",
        object_ids=tuple(object_ids or ()),
        objects_file=objects_file,
        batch_id=batch_id,
        category=category,
        allow_missing_metadata=False,
    )


def timestamp_batch_inventory(
    ctx: typer.Context,
    object_ids: Annotated[
        list[str] | None,
        typer.Option("--object", help="Object ID to include in a new batch."),
    ] = None,
    objects_file: Annotated[
        Path | None,
        typer.Option("--objects-file", help="File containing object IDs."),
    ] = None,
    batch_id: Annotated[
        str | None,
        typer.Option("--batch", help="Resume an existing batch."),
    ] = None,
    category: Annotated[
        int | None,
        typer.Option("--category", help="SimpleProof category for a new batch."),
    ] = None,
    allow_missing_metadata: Annotated[
        bool,
        typer.Option(
            "--allow-missing-metadata",
            help="Timestamp inventory even when staged metadata is incomplete.",
        ),
    ] = False,
    upgrade: Annotated[
        bool,
        typer.Option("--upgrade", help="Poll SP verify jobs and ingest proofs."),
    ] = False,
    retry: Annotated[
        bool,
        typer.Option("--retry", help="Repair and resubmit failed batch chunks."),
    ] = False,
    watch_flag: Annotated[
        bool,
        typer.Option(
            "--watch",
            help="ZMQ-driven loop; runs a batch upgrade pass per Bitcoin block.",
        ),
    ] = False,
) -> None:
    """Create or resume an inventory timestamping batch."""
    if retry and (upgrade or watch_flag):
        raise UsageError("--retry is mutually exclusive with --upgrade and --watch")
    if upgrade and watch_flag:
        raise UsageError("--upgrade is mutually exclusive with --watch")
    if retry:
        if (
            object_ids
            or objects_file is not None
            or category is not None
            or allow_missing_metadata
        ):
            raise UsageError("--retry is only supported with --batch")
        _drive_batch_retry(ctx, target="inventory", batch_id=batch_id)
        return
    if watch_flag:
        _drive_batch_watch(
            ctx,
            target="inventory",
            object_ids=tuple(object_ids or ()),
            objects_file=objects_file,
            batch_id=batch_id,
            category=category,
            allow_missing_metadata=allow_missing_metadata,
        )
        return
    if upgrade:
        if object_ids or objects_file is not None or category is not None:
            raise UsageError("--upgrade is only supported with --batch")
        _drive_batch_upgrade(ctx, target="inventory", batch_id=batch_id)
        return
    _drive_batch_submit(
        ctx,
        target="inventory",
        object_ids=tuple(object_ids or ()),
        objects_file=objects_file,
        batch_id=batch_id,
        category=category,
        allow_missing_metadata=allow_missing_metadata,
    )


def timestamp_batch_ls(ctx: typer.Context) -> None:
    """List local batch timestamping state files."""
    staging_root = stage_store(ctx)._root
    rows = [_batch_to_list_row(batch) for batch in _load_batches_or_exit(staging_root)]
    if appctx.json_mode(ctx):
        output_json(rows)
        return
    for row in rows:
        typer.echo(
            f"{row['short_batch_id']}  {row['target']}  "
            f"created={row['created_at']} updated={row['updated_at']}  "
            f"prepared={row['prepared']} submitted={row['submitted']} "
            f"upgraded={row['upgraded']} failed={row['failed']} "
            f"skipped={row['skipped']} total={row['total']}"
        )


def timestamp_batch_status(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Argument(help="Batch ID.")],
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="Show per-digest submit/verify state."),
    ] = False,
) -> None:
    """Show local per-object status for a batch."""
    out = appctx.console(ctx)
    staging_root = stage_store(ctx)._root
    batch = _load_batch_or_usage(ctx, staging_root, batch_id)
    data = _batch_status_dict(staging_root, batch, verbose=verbose)
    if appctx.json_mode(ctx):
        output_json(data)
        return
    render_section(out, "Batch")
    typer.echo(f"  ID: {batch.batch_id}")
    typer.echo(f"  Target: {batch.target}")
    typer.echo(f"  Category: {batch.category_id}")
    typer.echo(f"  Updated: {batch.updated_at}")
    render_section(out, "Objects", leading_blank=True)
    for row in data["objects"]:
        typer.echo(
            f"  {row['object_id']}  {row['batch_status']}  "
            f"{row['digests_verified']}/{row['digests_total']} verified  "
            f"Next: {row['next']}"
        )
        if verbose:
            for digest in row["digests"]:
                typer.echo(
                    f"    {digest['digest']}  submitted={digest['submitted']} "
                    f"verified={digest['verified']} "
                    f"submit={digest['submit_status']} "
                    f"verify={digest['verify_status']}"
                )


def timestamp_batch_rm(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Argument(help="Batch ID.")],
    force: Annotated[
        bool,
        typer.Option("--force", help="Delete the batch file even with open rows."),
    ] = False,
) -> None:
    """Delete a local batch file when cleanup is safe."""
    staging_root = stage_store(ctx)._root
    batch = _load_batch_or_usage(ctx, staging_root, batch_id)
    open_rows = [
        row for row in batch.objects if row.status in {"prepared", "submitted"}
    ]
    if open_rows and not force:
        raise CLIError(
            f"batch {batch_id} has non-terminal rows; use --force to delete "
            "the batch file only"
        )
    batch_state.batch_file_path(staging_root, batch_id).unlink()
    typer.echo(f"removed batch {batch_id}")


# ---------------------------------------------------------------------------
# reset.
# ---------------------------------------------------------------------------


__all__ = ["stage_app"]
