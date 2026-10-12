"""Batch watch: re-verify rows when the chain moves."""

from pathlib import Path  # noqa: TC003 - Typer reads option annotations at runtime.
from typing import TYPE_CHECKING

import msgspec

from ocfl_cli import errors as errors
from ocfl_cli.errors import (
    AttestationMismatchError,
    ReorgDetectedError,
)
from ocfl_cli.model.ots_state import OtsState, VerifiedProof
from ocfl_cli.model.phase import Phase
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

if TYPE_CHECKING:
    from ocfl_cli.model import batch as batch_model
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


def reverify_batch_content_row(
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


def reverify_batch_inventory_row(
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


def reset_batch_row_phase(target: batch_model.BatchTarget, state: OtsState) -> OtsState:
    phase = Phase.CONTENT_PENDING if target == "content" else Phase.INVENTORY_PENDING
    return msgspec.structs.replace(state, phase=phase)


def reverify_batch_watch_rows(
    *,
    target: batch_model.BatchTarget,
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
            if target == "content":
                state, row_reset, row_reorg = reverify_batch_content_row(
                    btc=btc,
                    staging_dir=staging_dir,
                    state=state,
                    row=row,
                    min_confirmations=cfg.bitcoin.confirmations,
                )
            else:
                state, row_reset, row_reorg = reverify_batch_inventory_row(
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
