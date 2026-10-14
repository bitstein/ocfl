"""Batch submission to SimpleProof, chunk by chunk, mirrored into each object."""

from typing import TYPE_CHECKING

import msgspec

from ocfl_cli import errors as errors
from ocfl_cli.errors import (
    SimpleProofError,
)
from ocfl_cli.model import batch as batch_model
from ocfl_cli.store import batch as batch_state
from ocfl_cli.store.ots_state import (
    load_ots,
    save_ots,
    with_lock,
)
from ocfl_cli.workflow.batch.reasons import (
    fixed_batch_reason,
)
from ocfl_cli.workflow.targets import rules_for

if TYPE_CHECKING:
    from pathlib import Path

    from ocfl_cli.model.ots_state import OtsState, Submission
    from ocfl_cli.model.phase import TimestampTarget
    from ocfl_cli.ports import Runtime
    from ocfl_cli.store.staging import StagingStore


def ensure_submit_chunks(
    batch: batch_model.BatchFile, *, chunk_size: int, runtime: Runtime
) -> batch_model.BatchFile:
    if batch.submissions:
        return batch
    digests = sorted(
        {d for row in batch.objects if row.status == "prepared" for d in row.digests}
    )
    chunks = tuple(
        batch_model.BatchSubmission(
            chunk_index=i // chunk_size,
            digests=tuple(digests[i : i + chunk_size]),
            status="pending",
        )
        for i in range(0, len(digests), chunk_size)
    )
    return msgspec.structs.replace(
        batch, submissions=chunks, updated_at=batch_state.utc_iso_z(runtime.clock.now())
    )


def row_by_object(batch: batch_model.BatchFile) -> dict[str, batch_model.BatchObject]:
    return {row.object_id: row for row in batch.objects}


def chunk_object_ids(
    batch: batch_model.BatchFile, chunk: batch_model.BatchSubmission
) -> list[str]:
    chunk_digests = set(chunk.digests)
    return [
        row.object_id
        for row in batch.objects
        if row.status in {"prepared", "submitted"}
        and chunk_digests.intersection(row.digests)
    ]


def mirror_chunk_to_object(
    *,
    target: TimestampTarget,
    store: StagingStore,
    root_path: str,
    staging_root: Path,
    batch: batch_model.BatchFile,
    object_id: str,
    ids_by_digest: dict[str, str],
    runtime: Runtime,
) -> bool:
    staging_dir = store.staging_path(root_path, object_id)
    with with_lock(staging_dir, runtime):
        state = load_ots(staging_dir)
        changed: list[Submission] = []
        completed = False
        for submission in state.submissions:
            if submission.batch_id != batch.batch_id or submission.target is not target:
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
        phase = rules_for(target).pending_phase if completed else state.phase
        save_ots(
            staging_dir,
            msgspec.structs.replace(state, submissions=tuple(changed), phase=phase),
        )
        return completed


def submit_batch(
    *,
    target: TimestampTarget,
    cfg,
    store: StagingStore,
    staging_root: Path,
    batch: batch_model.BatchFile,
    sp_factory,
    runtime: Runtime,
) -> batch_model.BatchFile:
    batch = ensure_submit_chunks(
        batch, chunk_size=sp_factory(cfg).submit_chunk_size, runtime=runtime
    )
    batch_state.save_batch(staging_root, batch)
    sp = sp_factory(cfg)
    rows = row_by_object(batch)
    submissions = list(batch.submissions)
    for index, chunk in enumerate(submissions):
        if chunk.status == "failed":
            continue
        if chunk.status == "submitted" and chunk.record_ids is not None:
            ids_by_digest = dict(zip(chunk.digests, chunk.record_ids, strict=True))
        else:
            chunk, rows, object_ids = revalidate_submit_chunk(
                target=target,
                store=store,
                batch=batch,
                chunk=chunk,
                rows=rows,
                runtime=runtime,
            )
            if not object_ids:
                submissions[index] = msgspec.structs.replace(
                    chunk,
                    status="submitted",
                    record_ids=(),
                )
                batch = save_batch_submit_progress(
                    staging_root, batch, rows, submissions, runtime=runtime
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
                        last_error=fixed_batch_reason("transient_sp_error"),
                    )
                return save_batch_submit_progress(
                    staging_root, batch, rows, submissions, runtime=runtime
                )
            if len(result.record_ids) != len(chunk.digests):
                failed = msgspec.structs.replace(
                    chunk,
                    status="failed",
                    attempts=chunk.attempts + 1,
                    last_error=fixed_batch_reason("missing_record_ids"),
                )
                submissions[index] = failed
                for object_id in object_ids:
                    rows[object_id] = msgspec.structs.replace(
                        rows[object_id],
                        last_error=fixed_batch_reason("missing_record_ids"),
                    )
                continue
            ids_by_digest = dict(zip(chunk.digests, result.record_ids, strict=True))
            submissions[index] = msgspec.structs.replace(
                chunk,
                status="submitted",
                record_ids=result.record_ids,
                submitted_at=batch_state.utc_iso_z(runtime.clock.now()),
                attempts=chunk.attempts + 1,
            )
            batch = msgspec.structs.replace(
                batch,
                objects=tuple(rows.values()),
                submissions=tuple(submissions),
                updated_at=batch_state.utc_iso_z(runtime.clock.now()),
            )
            batch_state.save_batch(staging_root, batch)

        for object_id in chunk_object_ids(batch, chunk):
            if mirror_chunk_to_object(
                target=target,
                store=store,
                root_path=batch.root_path,
                staging_root=staging_root,
                batch=batch,
                object_id=object_id,
                ids_by_digest=ids_by_digest,
                runtime=runtime,
            ):
                rows[object_id] = msgspec.structs.replace(
                    rows[object_id], status="submitted", last_error=None
                )
    return save_batch_submit_progress(
        staging_root, batch, rows, submissions, runtime=runtime
    )


def save_batch_submit_progress(
    staging_root: Path,
    batch: batch_model.BatchFile,
    rows: dict[str, batch_model.BatchObject],
    submissions: list[batch_model.BatchSubmission],
    *,
    runtime: Runtime,
) -> batch_model.BatchFile:
    batch = msgspec.structs.replace(
        batch,
        objects=tuple(rows.values()),
        submissions=tuple(submissions),
        updated_at=batch_state.utc_iso_z(runtime.clock.now()),
    )
    batch_state.save_batch(staging_root, batch)
    return batch


def revalidate_submit_chunk(
    *,
    target: TimestampTarget,
    store: StagingStore,
    batch: batch_model.BatchFile,
    chunk: batch_model.BatchSubmission,
    rows: dict[str, batch_model.BatchObject],
    runtime: Runtime,
) -> tuple[
    batch_model.BatchSubmission,
    dict[str, batch_model.BatchObject],
    list[str],
]:
    valid_digests = set(chunk.digests)
    valid_object_ids: list[str] = []
    for object_id in chunk_object_ids(batch, chunk):
        staging_dir = store.staging_path(batch.root_path, object_id)
        with with_lock(staging_dir, runtime):
            state = load_ots(staging_dir)
            if state.category_id != batch.category_id:
                rows[object_id] = msgspec.structs.replace(
                    rows[object_id],
                    status="failed",
                    last_error=fixed_batch_reason("regressed"),
                )
                valid_digests.difference_update(rows[object_id].digests)
                continue
            row_digests = revalidate_submission(
                state, batch.batch_id, target, rows[object_id]
            )
            if row_digests is None:
                rows[object_id] = msgspec.structs.replace(
                    rows[object_id],
                    status="failed",
                    last_error=fixed_batch_reason("regressed"),
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


def revalidate_submission(
    state: OtsState,
    batch_id: str,
    target: TimestampTarget,
    row: batch_model.BatchObject,
) -> set[str] | None:
    """The row's digests still to send; None when the object moved on."""
    for submission in state.submissions:
        if submission.batch_id != batch_id or submission.target is not target:
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
