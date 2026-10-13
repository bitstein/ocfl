"""Batch retry: resubmit the failed chunks."""

from pathlib import Path  # noqa: TC003 - Typer reads option annotations at runtime.
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
    with_lock,
)
from ocfl_cli.workflow.batch.reasons import fixed_batch_reason
from ocfl_cli.workflow.batch.report import object_record_ids_by_digest
from ocfl_cli.workflow.batch.submit import (
    ensure_submit_chunks,
    mirror_chunk_to_object,
    revalidate_submit_chunk,
    row_by_object,
    save_batch_submit_progress,
)

if TYPE_CHECKING:
    from ocfl_cli.model.phase import TimestampTarget
    from ocfl_cli.ports import Runtime
    from ocfl_cli.store.staging import StagingStore


def object_ids_for_digests(
    batch: batch_model.BatchFile, digests: tuple[str, ...]
) -> list[str]:
    wanted = set(digests)
    return [
        row.object_id
        for row in batch.objects
        if row.status in {"prepared", "submitted"} and wanted.intersection(row.digests)
    ]


def collect_retry_record_ids(
    *,
    target: TimestampTarget,
    store: StagingStore,
    staging_root: Path,
    batch: batch_model.BatchFile,
    submissions: list[batch_model.BatchSubmission],
    runtime: Runtime,
) -> tuple[dict[str, str], dict[str, batch_model.BatchObject]]:
    rows = row_by_object(batch)
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
        with with_lock(staging_dir, timeout=runtime.lock_timeout):
            state = load_ots(staging_dir)
            for digest, record_id in object_record_ids_by_digest(
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
        if row_ids and mirror_chunk_to_object(
            target=target,
            store=store,
            root_path=batch.root_path,
            staging_root=staging_root,
            batch=batch,
            object_id=row.object_id,
            ids_by_digest=row_ids,
            runtime=runtime,
        ):
            rows[row.object_id] = msgspec.structs.replace(
                row, status="submitted", last_error=None
            )
    return ids_by_digest, rows


def append_retry_chunk(
    submissions: list[batch_model.BatchSubmission],
    *,
    digests: tuple[str, ...],
    status: batch_model.BatchSubmissionStatus,
    record_ids: tuple[str, ...] | None = None,
    attempts: int = 0,
    last_error: str | None = None,
    runtime: Runtime,
) -> batch_model.BatchSubmission:
    next_index = (
        max((submission.chunk_index for submission in submissions), default=-1) + 1
    )
    chunk = batch_model.BatchSubmission(
        chunk_index=next_index,
        digests=digests,
        status=status,
        record_ids=record_ids,
        submitted_at=batch_state.utc_iso_z(runtime.clock.now())
        if status == "submitted"
        else None,
        attempts=attempts,
        last_error=last_error,
    )
    submissions.append(chunk)
    return chunk


def missing_prepared_retry_digests(
    *,
    target: TimestampTarget,
    store: StagingStore,
    batch: batch_model.BatchFile,
    rows: dict[str, batch_model.BatchObject],
    ids_by_digest: dict[str, str],
    source_digests: set[str],
    runtime: Runtime,
) -> tuple[str, ...]:
    missing: list[str] = []
    seen: set[str] = set(source_digests)
    for row in batch.objects:
        if rows[row.object_id].status != "prepared":
            continue
        staging_dir = store.staging_path(batch.root_path, row.object_id)
        with with_lock(staging_dir, timeout=runtime.lock_timeout):
            state = load_ots(staging_dir)
            object_ids = object_record_ids_by_digest(batch, row, state)
        for digest in row.digests:
            if digest in seen:
                continue
            if ids_by_digest.get(digest) is None and object_ids.get(digest) is None:
                missing.append(digest)
                seen.add(digest)
    return tuple(missing)


def save_retry_pre_post(
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
    with batch_state.with_batch_lock(staging_root, batch.batch_id):
        batch_state.save_batch(staging_root, batch)
    return batch


def retry_batch_submissions(
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
    submissions = list(batch.submissions)
    ids_by_digest, rows = collect_retry_record_ids(
        target=target,
        store=store,
        staging_root=staging_root,
        batch=batch,
        submissions=submissions,
        runtime=runtime,
    )
    retry_sources = [
        (index, chunk)
        for index, chunk in enumerate(submissions)
        if chunk.status in {"failed", "pending"}
    ]
    source_digests = {
        digest for _index, chunk in retry_sources for digest in chunk.digests
    }
    for digest in missing_prepared_retry_digests(
        target=target,
        store=store,
        batch=batch,
        rows=rows,
        ids_by_digest=ids_by_digest,
        source_digests=source_digests,
        runtime=runtime,
    ):
        retry_sources.append(
            (
                len(submissions),
                append_retry_chunk(
                    submissions,
                    digests=(digest,),
                    status="pending",
                    runtime=runtime,
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
                    submitted_at=batch_state.utc_iso_z(runtime.clock.now()),
                )
                submissions[source_index] = repair_chunk
            else:
                repair_chunk = append_retry_chunk(
                    submissions,
                    digests=tuple(digest for digest, _record_id in repaired),
                    status="submitted",
                    record_ids=tuple(record_id for _digest, record_id in repaired),
                    runtime=runtime,
                )
            for object_id in object_ids_for_digests(batch, repair_chunk.digests):
                if mirror_chunk_to_object(
                    target=target,
                    store=store,
                    root_path=batch.root_path,
                    staging_root=staging_root,
                    batch=batch,
                    object_id=object_id,
                    ids_by_digest=dict(repaired),
                    runtime=runtime,
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
        retry_chunk, rows, object_ids = revalidate_submit_chunk(
            target=target,
            store=store,
            batch=msgspec.structs.replace(batch, submissions=tuple(submissions)),
            chunk=retry_chunk,
            rows=rows,
            runtime=runtime,
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
        batch = save_retry_pre_post(
            staging_root, batch, rows, submissions, runtime=runtime
        )
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
                    last_error=fixed_batch_reason("transient_sp_error"),
                )
            return save_batch_submit_progress(
                staging_root, batch, rows, submissions, runtime=runtime
            )
        if len(result.record_ids) != len(retry_chunk.digests):
            submissions[retry_index] = msgspec.structs.replace(
                retry_chunk,
                status="failed",
                last_error=fixed_batch_reason("missing_record_ids"),
            )
            for object_id in object_ids:
                rows[object_id] = msgspec.structs.replace(
                    rows[object_id],
                    last_error=fixed_batch_reason("missing_record_ids"),
                )
            continue
        retry_ids = dict(zip(retry_chunk.digests, result.record_ids, strict=True))
        ids_by_digest.update(retry_ids)
        retry_chunk = msgspec.structs.replace(
            retry_chunk,
            status="submitted",
            record_ids=result.record_ids,
            submitted_at=batch_state.utc_iso_z(runtime.clock.now()),
        )
        submissions[retry_index] = retry_chunk
        for object_id in object_ids:
            if mirror_chunk_to_object(
                target=target,
                store=store,
                root_path=batch.root_path,
                staging_root=staging_root,
                batch=batch,
                object_id=object_id,
                ids_by_digest=retry_ids,
                runtime=runtime,
            ):
                rows[object_id] = msgspec.structs.replace(
                    rows[object_id], status="submitted", last_error=None
                )
    return save_batch_submit_progress(
        staging_root, batch, rows, submissions, runtime=runtime
    )
