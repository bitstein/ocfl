"""Batch watch: re-verify rows when the chain moves."""

from pathlib import Path  # noqa: TC003 - Typer reads option annotations at runtime.
from typing import TYPE_CHECKING

import msgspec

from ocfl_cli import errors as errors
from ocfl_cli.errors import (
    AttestationMismatchError,
    ReorgDetectedError,
)
from ocfl_cli.ots.bitcoin import (
    PendingVerification,
    VerifiedAttestation,
    verify_attestation,
)
from ocfl_cli.store.ots_state import (
    load_ots,
    ots_cache_path,
    save_ots,
    with_lock,
)
from ocfl_cli.workflow.batch.reasons import (
    fixed_batch_reason,
)
from ocfl_cli.workflow.batch.upgrade import (
    save_batch_upgrade,
)
from ocfl_cli.workflow.common import unverify, verified_proof
from ocfl_cli.workflow.targets import rules_for

if TYPE_CHECKING:
    from ocfl_cli.model import batch as batch_model
    from ocfl_cli.model.ots_state import OtsState
    from ocfl_cli.model.phase import TimestampTarget
    from ocfl_cli.ports import Runtime
    from ocfl_cli.store.staging import StagingStore


def batch_has_submitted_subset(batch: batch_model.BatchFile) -> bool:
    return any(row.status == "submitted" for row in batch.objects)


def batch_watch_complete(batch: batch_model.BatchFile) -> bool:
    return bool(batch.objects) and all(
        row.status in {"upgraded", "failed", "skipped"} for row in batch.objects
    )


def batch_watch_successful(batch: batch_model.BatchFile) -> bool:
    return batch_watch_complete(batch) and any(
        row.status == "upgraded" for row in batch.objects
    )


def reverify_batch_row(
    *,
    btc,
    staging_dir: Path,
    state: OtsState,
    row: batch_model.BatchObject,
    min_confirmations: int,
) -> tuple[OtsState, set[str], set[str]]:
    """Re-verify the row's verified proofs; `(state, reset, reorged)` digests."""
    proofs = dict(state.proofs)
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
            proofs[digest] = unverify(proof)
            reset.add(digest)
            continue
        except ReorgDetectedError:
            proofs[digest] = unverify(proof)
            reorg.add(digest)
            continue
        match result:
            case VerifiedAttestation():
                proofs[digest] = verified_proof(
                    proof.target, proof.ots_path, result, proof.verified_at
                )
            case PendingVerification():
                proofs[digest] = unverify(proof)
                reset.add(digest)
    if proofs == state.proofs:
        return state, reset, reorg
    return msgspec.structs.replace(state, proofs=proofs), reset, reorg


def reset_batch_row_phase(target: TimestampTarget, state: OtsState) -> OtsState:
    phase = rules_for(target).pending_phase
    return msgspec.structs.replace(state, phase=phase)


def reverify_batch_watch_rows(
    *,
    target: TimestampTarget,
    cfg,
    store: StagingStore,
    staging_root: Path,
    batch: batch_model.BatchFile,
    btc,
    runtime: Runtime,
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
        with with_lock(staging_dir, timeout=runtime.lock_timeout):
            state = load_ots(staging_dir)
            state, row_reset, row_reorg = reverify_batch_row(
                btc=btc,
                staging_dir=staging_dir,
                state=state,
                row=row,
                min_confirmations=cfg.bitcoin.confirmations,
            )
            if row_reorg:
                state = reset_batch_row_phase(target, state)
                row = msgspec.structs.replace(
                    row,
                    status="failed",
                    last_error=fixed_batch_reason("reorg_deeper_than_min_conf"),
                )
                changed_rows = True
            if row_reset:
                reset.update(row_reset)
                state = reset_batch_row_phase(target, state)
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
        return save_batch_upgrade(
            staging_root,
            msgspec.structs.replace(batch, objects=tuple(rows)),
            runtime=runtime,
        )
    kept_jobs = tuple(job for job in batch.verify_jobs if reset.isdisjoint(job.digests))
    return save_batch_upgrade(
        staging_root,
        msgspec.structs.replace(batch, objects=tuple(rows), verify_jobs=kept_jobs),
        runtime=runtime,
    )
