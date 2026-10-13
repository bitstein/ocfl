"""Batch upgrade: verify jobs, download proofs, verify them against Bitcoin."""

from pathlib import Path  # noqa: TC003 - Typer reads option annotations at runtime.
from typing import TYPE_CHECKING

import msgspec

from ocfl_cli import errors as errors
from ocfl_cli.errors import (
    AttestationMismatchError,
    ReorgDetectedError,
    SimpleProofBadRequest,
    SimpleProofError,
    SimpleProofTransient,
)
from ocfl_cli.model import batch as batch_model
from ocfl_cli.ots.simpleproof import VERIFY_BATCH_CHUNK_SIZE
from ocfl_cli.store import batch as batch_state
from ocfl_cli.store.ots_state import (
    load_ots,
    save_ots,
    with_lock,
)
from ocfl_cli.workflow import timestamp
from ocfl_cli.workflow.batch.reasons import (
    fixed_batch_reason,
)
from ocfl_cli.workflow.targets import Area, rules_for
from ocfl_lib import BitcoinRpcError

if TYPE_CHECKING:
    from ocfl_cli.model.ots_state import OtsState
    from ocfl_cli.model.phase import TimestampTarget
    from ocfl_cli.model.simpleproof import SimpleProofDownloadResult
    from ocfl_cli.ports import Runtime
    from ocfl_cli.store.staging import StagingStore


def batch_digest_verified(
    *, target: TimestampTarget, state: OtsState, digest: str
) -> bool:
    """Whether `digest` is proved for the object's staged version."""
    return rules_for(target).row_upgraded(state, (digest,))


def covered_verify_digests(batch: batch_model.BatchFile) -> set[str]:
    return {
        digest
        for job in batch.verify_jobs
        if job.status in batch_model.BATCH_VERIFY_ACTIVE_STATUSES
        or (
            job.status == "COMPLETED"
            and job.last_error != fixed_batch_reason("sp_partial_results")
        )
        for digest in job.digests
    }


def batch_unverified_submitted_digests(
    *,
    target: TimestampTarget,
    store: StagingStore,
    batch: batch_model.BatchFile,
    runtime: Runtime,
) -> tuple[str, ...]:
    covered = covered_verify_digests(batch)
    pending: list[str] = []
    seen: set[str] = set()
    for row in batch.objects:
        if row.status != "submitted":
            continue
        staging_dir = store.staging_path(batch.root_path, row.object_id)
        with with_lock(staging_dir, timeout=runtime.lock_timeout):
            state = load_ots(staging_dir)
            for digest in row.digests:
                if digest in seen or digest in covered:
                    continue
                if not batch_digest_verified(target=target, state=state, digest=digest):
                    pending.append(digest)
                    seen.add(digest)
    return tuple(pending)


def save_batch_upgrade(
    staging_root: Path, batch: batch_model.BatchFile, *, runtime: Runtime
) -> batch_model.BatchFile:
    batch = msgspec.structs.replace(
        batch, updated_at=batch_state.utc_iso_z(runtime.clock.now())
    )
    with batch_state.with_batch_lock(staging_root, batch.batch_id):
        batch_state.save_batch(staging_root, batch)
    return batch


def submit_batch_verify_jobs(
    *,
    target: TimestampTarget,
    cfg,
    store: StagingStore,
    staging_root: Path,
    batch: batch_model.BatchFile,
    sp_factory,
    runtime: Runtime,
) -> batch_model.BatchFile:
    sp = sp_factory(cfg)
    digests = batch_unverified_submitted_digests(
        target=target, store=store, batch=batch, runtime=runtime
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
                submitted_at=batch_state.utc_iso_z(runtime.clock.now()),
                attempts=1,
                last_error=str(e),
            )
            jobs.append(failed)
            rows = tuple(
                msgspec.structs.replace(
                    row, last_error=fixed_batch_reason("transient_sp_error")
                )
                if row.status == "submitted" and set(row.digests).intersection(chunk)
                else row
                for row in batch.objects
            )
            batch = msgspec.structs.replace(
                batch, objects=rows, verify_jobs=tuple(jobs)
            )
            return save_batch_upgrade(staging_root, batch, runtime=runtime)
        jobs.append(
            batch_model.BatchVerifyJob(
                job_id=job_id,
                digests=chunk,
                status="IN_PROGRESS",
                submitted_at=batch_state.utc_iso_z(runtime.clock.now()),
                attempts=1,
            )
        )
        batch = msgspec.structs.replace(batch, verify_jobs=tuple(jobs))
        batch = save_batch_upgrade(staging_root, batch, runtime=runtime)
    return batch


def poll_batch_verify_jobs(
    *,
    cfg,
    staging_root: Path,
    batch: batch_model.BatchFile,
    sp_factory,
    runtime: Runtime,
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
                    completed_at=batch_state.utc_iso_z(runtime.clock.now()),
                    last_error=str(e),
                )
            )
            affected = set(job.digests)
            rows = [
                msgspec.structs.replace(
                    row, last_error=fixed_batch_reason("transient_sp_error")
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
                    completed_at=batch_state.utc_iso_z(runtime.clock.now()),
                    last_error=str(e),
                )
            )
            affected = set(job.digests)
            rows = [
                msgspec.structs.replace(
                    row, last_error=fixed_batch_reason("verify_job_failed")
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
                batch_state.utc_iso_z(runtime.clock.now())
                if status in batch_model.BATCH_VERIFY_TERMINAL_STATUSES
                else None
            ),
            last_error=(
                fixed_batch_reason("verify_job_failed")
                if status == "FAILED"
                else fixed_batch_reason("results_expired")
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
                    row, last_error=fixed_batch_reason("verify_job_failed")
                )
                if row.status == "submitted" and affected.intersection(row.digests)
                else row
                for row in rows
            ]
        if status == "EXPIRED":
            affected = set(job.digests)
            rows = [
                msgspec.structs.replace(
                    row, last_error=fixed_batch_reason("results_expired")
                )
                if row.status == "submitted" and affected.intersection(row.digests)
                else row
                for row in rows
            ]
    if not changed:
        return batch
    return save_batch_upgrade(
        staging_root,
        msgspec.structs.replace(batch, objects=tuple(rows), verify_jobs=tuple(jobs)),
        runtime=runtime,
    )


def ingest_batch_downloads(
    *,
    target: TimestampTarget,
    store: StagingStore,
    sr,
    batch: batch_model.BatchFile,
    row: batch_model.BatchObject,
    downloads: SimpleProofDownloadResult,
    btc,
    min_confirmations: int,
    runtime: Runtime,
) -> tuple[batch_model.BatchObject, bool]:
    """Verify and record one row's downloaded proofs; advance its phase when done."""
    rules = rules_for(target)
    staging_dir = store.staging_path(batch.root_path, row.object_id)
    area = Area(
        store=store,
        sr=sr,
        root_path=batch.root_path,
        object_id=row.object_id,
        staging_dir=staging_dir,
    )
    with with_lock(staging_dir, timeout=runtime.lock_timeout):
        state = load_ots(staging_dir)
        if not rules.row_current(area, state, row.digests):
            return (
                msgspec.structs.replace(
                    row, status="failed", last_error=fixed_batch_reason("regressed")
                ),
                True,
            )
        for digest in row.digests:
            ots_bytes = downloads.get(digest)
            if ots_bytes is None or state.verified(digest):
                continue
            state = timestamp.verify_digest(
                digest,
                ots_bytes,
                state,
                target,
                btc=btc,
                staging_dir=staging_dir,
                min_confirmations=min_confirmations,
                runtime=runtime,
            )
        if rules.progress(area, state).done and state.phase is not rules.upgraded_phase:
            state = msgspec.structs.replace(state, phase=rules.upgraded_phase)
            save_ots(staging_dir, state)
        if rules.row_upgraded(state, row.digests):
            return (
                msgspec.structs.replace(row, status="upgraded", last_error=None),
                True,
            )
    return row, False


def ingest_batch_verify_jobs(
    *,
    target: TimestampTarget,
    cfg,
    store: StagingStore,
    sr,
    staging_root: Path,
    batch: batch_model.BatchFile,
    sp_factory,
    btc_factory,
    runtime: Runtime,
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
                        row, last_error=fixed_batch_reason("results_expired")
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
                        row, last_error=fixed_batch_reason("transient_sp_error")
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
                last_error=fixed_batch_reason("attestation_mismatch"),
            )
            affected = set(job.digests)
            rows = {
                object_id: (
                    msgspec.structs.replace(
                        row,
                        status="failed",
                        last_error=fixed_batch_reason("attestation_mismatch"),
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
                new_row, row_changed = ingest_batch_downloads(
                    target=target,
                    store=store,
                    sr=sr,
                    batch=batch,
                    row=row,
                    downloads=downloads,
                    btc=btc,
                    min_confirmations=min_confirmations,
                    runtime=runtime,
                )
            except AttestationMismatchError:
                new_row = msgspec.structs.replace(
                    row,
                    status="failed",
                    last_error=fixed_batch_reason("attestation_mismatch"),
                )
                row_changed = True
            except ReorgDetectedError:
                new_row = msgspec.structs.replace(
                    row,
                    status="failed",
                    last_error=fixed_batch_reason("reorg_deeper_than_min_conf"),
                )
                row_changed = True
            except BitcoinRpcError:
                new_row = msgspec.structs.replace(
                    row, last_error=fixed_batch_reason("transient_rpc_error")
                )
                row_changed = True
            if row_changed:
                rows[row.object_id] = new_row
                changed = True
        if missing:
            jobs[index] = msgspec.structs.replace(
                jobs[index], last_error=fixed_batch_reason("sp_partial_results")
            )
            rows = {
                object_id: (
                    msgspec.structs.replace(
                        row, last_error=fixed_batch_reason("sp_partial_results")
                    )
                    if row.status == "submitted" and missing.intersection(row.digests)
                    else row
                )
                for object_id, row in rows.items()
            }
            changed = True
    if not changed:
        return batch
    return save_batch_upgrade(
        staging_root,
        msgspec.structs.replace(
            batch,
            objects=tuple(rows.values()),
            verify_jobs=tuple(jobs),
        ),
        runtime=runtime,
    )


def reconcile_upgraded_batch_rows(
    *,
    target: TimestampTarget,
    store: StagingStore,
    staging_root: Path,
    batch: batch_model.BatchFile,
    runtime: Runtime,
) -> batch_model.BatchFile:
    rows: list[batch_model.BatchObject] = []
    changed = False
    for row in batch.objects:
        if row.status != "submitted":
            rows.append(row)
            continue
        staging_dir = store.staging_path(batch.root_path, row.object_id)
        with with_lock(staging_dir, timeout=runtime.lock_timeout):
            state = load_ots(staging_dir)
            upgraded = rules_for(target).row_upgraded(state, row.digests)
        if upgraded:
            rows.append(
                msgspec.structs.replace(row, status="upgraded", last_error=None)
            )
            changed = True
        else:
            rows.append(row)
    if not changed:
        return batch
    return save_batch_upgrade(
        staging_root,
        msgspec.structs.replace(batch, objects=tuple(rows)),
        runtime=runtime,
    )


def run_batch_upgrade_pass(
    *,
    target: TimestampTarget,
    cfg,
    store: StagingStore,
    sr,
    staging_root: Path,
    batch: batch_model.BatchFile,
    sp_factory,
    btc_factory,
    runtime: Runtime,
) -> batch_model.BatchFile:
    batch = submit_batch_verify_jobs(
        target=target,
        cfg=cfg,
        store=store,
        staging_root=staging_root,
        batch=batch,
        sp_factory=sp_factory,
        runtime=runtime,
    )
    batch = reconcile_upgraded_batch_rows(
        target=target,
        store=store,
        staging_root=staging_root,
        batch=batch,
        runtime=runtime,
    )
    batch = poll_batch_verify_jobs(
        cfg=cfg,
        staging_root=staging_root,
        batch=batch,
        sp_factory=sp_factory,
        runtime=runtime,
    )
    batch = ingest_batch_verify_jobs(
        target=target,
        cfg=cfg,
        store=store,
        sr=sr,
        staging_root=staging_root,
        batch=batch,
        sp_factory=sp_factory,
        btc_factory=btc_factory,
        runtime=runtime,
    )
    return reconcile_upgraded_batch_rows(
        target=target,
        store=store,
        staging_root=staging_root,
        batch=batch,
        runtime=runtime,
    )
