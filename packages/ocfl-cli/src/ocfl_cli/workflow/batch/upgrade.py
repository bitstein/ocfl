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
from ocfl_cli.model.phase import Phase
from ocfl_cli.ots.simpleproof import VERIFY_BATCH_CHUNK_SIZE
from ocfl_cli.store import batch as batch_state
from ocfl_cli.store.ots_state import (
    load_ots,
    plan_path,
    save_ots,
    with_lock,
)
from ocfl_cli.workflow import commit as wf_commit
from ocfl_cli.workflow import content as wf_content
from ocfl_cli.workflow import inventory as wf_inventory
from ocfl_cli.workflow.batch.reasons import (
    fixed_batch_reason,
)
from ocfl_lib import BitcoinRpcError

if TYPE_CHECKING:
    from ocfl_cli.model.ots_state import OtsState
    from ocfl_cli.model.simpleproof import SimpleProofDownloadResult
    from ocfl_cli.ports import Runtime
    from ocfl_cli.store.staging import StagingStore


def content_digest_verified(state: OtsState, digest: str) -> bool:
    return state.verified(digest)


def inventory_digest_verified(state: OtsState, digest: str) -> bool:
    return (
        state.inventory is not None
        and state.inventory.digest == digest
        and state.verified(digest)
    )


def batch_digest_verified(
    *, target: batch_model.BatchTarget, state: OtsState, digest: str
) -> bool:
    if target == "content":
        return content_digest_verified(state, digest)
    return inventory_digest_verified(state, digest)


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
    target: batch_model.BatchTarget,
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
    target: batch_model.BatchTarget,
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


def content_row_upgraded(state: OtsState, row: batch_model.BatchObject) -> bool:
    return all(content_digest_verified(state, digest) for digest in row.digests)


def inventory_row_upgraded(state: OtsState, row: batch_model.BatchObject) -> bool:
    return all(inventory_digest_verified(state, digest) for digest in row.digests)


def ingest_batch_content_downloads(
    *,
    store: StagingStore,
    sr,
    batch: batch_model.BatchFile,
    row: batch_model.BatchObject,
    downloads: SimpleProofDownloadResult,
    btc,
    min_confirmations: int,
    runtime: Runtime,
) -> tuple[batch_model.BatchObject, bool]:
    staging_dir = store.staging_path(batch.root_path, row.object_id)
    with with_lock(staging_dir, timeout=runtime.lock_timeout):
        state = load_ots(staging_dir)
        for digest in row.digests:
            ots_bytes = downloads.get(digest)
            if ots_bytes is None or content_digest_verified(state, digest):
                continue
            state = wf_content.verify_content_digest(
                digest,
                ots_bytes,
                state,
                btc=btc,
                staging_dir=staging_dir,
                min_confirmations=min_confirmations,
                runtime=runtime,
            )
        ready, _unverified_pending, _orphans = wf_content.phase_advance_ready(
            state, store, sr, batch.root_path, row.object_id, staging_dir
        )
        if ready and state.phase is not Phase.CONTENT_UPGRADED:
            state = msgspec.structs.replace(state, phase=Phase.CONTENT_UPGRADED)
            save_ots(staging_dir, state)
        if content_row_upgraded(state, row):
            return (
                msgspec.structs.replace(row, status="upgraded", last_error=None),
                True,
            )
    return row, False


def ingest_batch_inventory_downloads(
    *,
    store: StagingStore,
    sr,
    batch: batch_model.BatchFile,
    row: batch_model.BatchObject,
    downloads: SimpleProofDownloadResult,
    btc,
    min_confirmations: int,
    runtime: Runtime,
) -> tuple[batch_model.BatchObject, bool]:
    staging_dir = store.staging_path(batch.root_path, row.object_id)
    with with_lock(staging_dir, timeout=runtime.lock_timeout):
        state = load_ots(staging_dir)
        proof = state.inventory
        if proof is None or proof.batch_id != batch.batch_id:
            return (
                msgspec.structs.replace(
                    row, status="failed", last_error=fixed_batch_reason("regressed")
                ),
                True,
            )
        if not inventory_plan_matches_submission(
            store=store,
            sr=sr,
            batch=batch,
            row=row,
            staging_dir=staging_dir,
            state=state,
        ):
            return (
                msgspec.structs.replace(
                    row, status="failed", last_error=fixed_batch_reason("regressed")
                ),
                True,
            )
        ots_bytes = downloads.get(proof.digest)
        if ots_bytes is None or state.verified(proof.digest):
            return row, False
        state = wf_inventory.verify_inventory_digest(
            state, ots_bytes, btc, staging_dir, min_confirmations, runtime=runtime
        )
        if inventory_row_upgraded(state, row):
            return (
                msgspec.structs.replace(row, status="upgraded", last_error=None),
                True,
            )
    return row, False


def inventory_plan_matches_submission(
    *,
    store: StagingStore,
    sr,
    batch: batch_model.BatchFile,
    row: batch_model.BatchObject,
    staging_dir: Path,
    state: OtsState,
) -> bool:
    proof = state.inventory
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


def ingest_batch_verify_jobs(
    *,
    target: batch_model.BatchTarget,
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
                if target == "content":
                    new_row, row_changed = ingest_batch_content_downloads(
                        store=store,
                        sr=sr,
                        batch=batch,
                        row=row,
                        downloads=downloads,
                        btc=btc,
                        min_confirmations=min_confirmations,
                        runtime=runtime,
                    )
                else:
                    new_row, row_changed = ingest_batch_inventory_downloads(
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
    target: batch_model.BatchTarget,
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
            upgraded = (
                content_row_upgraded(state, row)
                if target == "content"
                else inventory_row_upgraded(state, row)
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
    return save_batch_upgrade(
        staging_root,
        msgspec.structs.replace(batch, objects=tuple(rows)),
        runtime=runtime,
    )


def run_batch_upgrade_pass(
    *,
    target: batch_model.BatchTarget,
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
