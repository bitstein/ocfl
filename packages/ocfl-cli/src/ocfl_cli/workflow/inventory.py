"""Inventory timestamping use cases: submit, upgrade, retry."""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from msgspec.structs import replace

from ocfl_cli.errors import (
    AttestationMismatchError,
    InventoryProofMissingError,
    PhaseError,
)
from ocfl_cli.model.ots_state import (
    InventoryProof,
    OtsState,
    VerifiedProof,
)
from ocfl_cli.model.phase import Phase, check_transition
from ocfl_cli.ots.bitcoin import (
    PendingVerification,
    VerifiedAttestation,
    verify_attestation,
)
from ocfl_cli.store import staging as _store_mod
from ocfl_cli.store.ots_state import (
    apply_category_id,
    load_ots,
    ots_cache_path,
    plan_path,
    save_ots,
    utc_iso,
    with_lock,
)
from ocfl_cli.workflow.commit import (
    build_inventory_plan,
    load_plan,
    require_staged_metadata,
    version_user_from_staged,
)
from ocfl_cli.workflow.common import (
    JobProgress,
    compute_inventory_job_progress,
    is_failed_verify_job,
    log_phase,
    poll_outstanding_jobs,
    require_verified_content_before_inventory,
    snapshot_results,
    verify_job_from_ack,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ocfl_cli.config import Config
    from ocfl_cli.ports import (
        BitcoinRPC,
        BtcRpcFactory,
        Runtime,
        SimpleProofClient,
        SpClientFactory,
    )
    from ocfl_cli.store.staging import StagingStore
    from ocfl_lib import (
        CommitPlan,
    )

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SubmitInventoryResult:
    submitted_count: int
    previous_phase: Phase
    phase: Phase


@dataclass(frozen=True, slots=True)
class PreparedInventorySubmission:
    digest: str | None
    category_id: int
    previous_phase: Phase
    target_phase: Phase
    plan: CommitPlan | None = None


@dataclass(frozen=True, slots=True)
class UpgradeInventoryResult:
    previous_phase: Phase
    phase: Phase
    job: JobProgress | None = None


def _reset_inv_proof_and_resubmit(
    state: OtsState,
    sp: SimpleProofClient,
    *,
    ots_path: str | None,
    category_id: int,
) -> OtsState:
    """Reset `inventory_proof.proof` to unverified and queue a fresh verify_batch."""
    if state.inventory_proof is None:
        return state
    inv_digest = state.inventory_proof.digest
    new_proof = VerifiedProof(ots_path=ots_path, verified=False)
    new_inv = replace(state.inventory_proof, proof=new_proof)
    kept_jobs = tuple(j for j in state.verify_jobs if inv_digest not in j.digests)
    new_job = verify_job_from_ack(
        sp.verify_batch([inv_digest], category_id),
        (inv_digest,),
    )
    return replace(state, inventory_proof=new_inv, verify_jobs=(*kept_jobs, new_job))


def reverify_inventory_proof(
    state: OtsState,
    btc: BitcoinRPC,
    sp: SimpleProofClient,
    staging_dir: Path,
    min_confirmations: int,
    category_id: int,
) -> OtsState:
    """Re-verify a `verified=True` ``inventory_proof.proof`` against the chain.

    Single-digest mirror of `reverify_content_proofs`. On mismatch /
    pending the proof resets to `verified=False` and a fresh verify_batch
    is queued; `ReorgDetectedError` propagates.
    """
    if state.inventory_proof is None:
        return state
    proof = state.inventory_proof.proof
    if not proof.verified:
        return state
    inv_digest = state.inventory_proof.digest
    cache = ots_cache_path(staging_dir, inv_digest)
    if not cache.is_file():
        return state
    try:
        result = verify_attestation(
            rpc=btc,
            ots_bytes=cache.read_bytes(),
            expected_digest=bytes.fromhex(inv_digest),
            min_confirmations=min_confirmations,
        )
    except AttestationMismatchError:
        return _reset_inv_proof_and_resubmit(
            state, sp, ots_path=proof.ots_path, category_id=category_id
        )

    match result:
        case VerifiedAttestation(block_height=h, confirmations=c):
            new_proof = VerifiedProof(
                ots_path=proof.ots_path,
                verified=True,
                block_height=h,
                block_hash=result.block_hash,
                confirmations_at_verify=c,
                verified_at=proof.verified_at,
            )
            new_inv = replace(state.inventory_proof, proof=new_proof)
            return replace(state, inventory_proof=new_inv)
        case PendingVerification():
            return _reset_inv_proof_and_resubmit(
                state, sp, ots_path=proof.ots_path, category_id=category_id
            )


def verify_inventory_digest(
    state: OtsState,
    ots_bytes: bytes,
    btc: BitcoinRPC,
    staging_dir: Path,
    min_confirmations: int,
    *,
    runtime: Runtime,
) -> OtsState:
    """Apply the three-step crash-recovery invariant for the inventory digest.

    Step 1: atomic cache write `ots-cache/<inv_digest>.ots`.
    Step 2: vacuous (extension-file is queued at `stage commit`).
    Step 3: atomic ots.json update — `inventory_proof.proof` set; on
    `VerifiedAttestation` `phase` advances to INVENTORY_UPGRADED in the
    same write so readers never see torn state.
    """
    inv_proof = state.inventory_proof
    if inv_proof is None:
        raise RuntimeError("inventory_proof missing")
    inv_digest = inv_proof.digest

    result = verify_attestation(
        rpc=btc,
        ots_bytes=ots_bytes,
        expected_digest=bytes.fromhex(inv_digest),
        min_confirmations=min_confirmations,
    )

    cache_file = ots_cache_path(staging_dir, inv_digest)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    _store_mod.atomic_write_bytes(cache_file, ots_bytes)
    ots_path_rel = f"ots-cache/{inv_digest}.ots"

    match result:
        case VerifiedAttestation(block_height=h, confirmations=c):
            new_proof = VerifiedProof(
                ots_path=ots_path_rel,
                verified=True,
                block_height=h,
                block_hash=result.block_hash,
                confirmations_at_verify=c,
                verified_at=utc_iso(runtime.clock.now()),
            )
            state = replace(
                state,
                inventory_proof=replace(inv_proof, proof=new_proof),
                phase=Phase.INVENTORY_UPGRADED,
            )
        case PendingVerification():
            new_proof = VerifiedProof(ots_path=ots_path_rel, verified=False)
            state = replace(state, inventory_proof=replace(inv_proof, proof=new_proof))

    save_ots(staging_dir, state)
    return state


def _process_completed_inventory_job(
    state: OtsState,
    sp: SimpleProofClient,
    btc: BitcoinRPC,
    staging_dir: Path,
    min_confirmations: int,
    *,
    runtime: Runtime,
) -> OtsState:
    """Download + verify any completed verify_job covering the inventory digest."""
    inv_proof = state.inventory_proof
    if inv_proof is None or inv_proof.proof.verified:
        return state
    inv_digest = inv_proof.digest

    for j in state.verify_jobs:
        if j.status != "COMPLETED" or inv_digest not in j.digests:
            continue
        poll = sp.poll_job(j.job_id)
        if poll.results_url is None:
            continue
        downloads = sp.download_results(poll.results_url, {inv_digest})
        if inv_digest not in downloads:
            continue
        return verify_inventory_digest(
            state,
            downloads[inv_digest],
            btc,
            staging_dir,
            min_confirmations,
            runtime=runtime,
        )
    return state


def prepare_inventory_upgrade_locked(
    state: OtsState,
    staging_dir: Path,
    *,
    sp: SimpleProofClient,
    btc: BitcoinRPC,
    category_id: int,
    min_confirmations: int,
) -> OtsState:
    """Reverify cached inventory proof and submit a missing verify job."""
    state = reverify_inventory_proof(
        state, btc, sp, staging_dir, min_confirmations, category_id
    )
    save_ots(staging_dir, state)

    inv_proof = state.inventory_proof
    if inv_proof is not None and not inv_proof.proof.verified:
        inv_digest = inv_proof.digest
        has_job = any(
            inv_digest in j.digests and not is_failed_verify_job(j.status)
            for j in state.verify_jobs
        )
        if not has_job:
            new_job = verify_job_from_ack(
                sp.verify_batch([inv_digest], category_id),
                (inv_digest,),
            )
            state = replace(state, verify_jobs=(*state.verify_jobs, new_job))
            save_ots(staging_dir, state)
    return state


def poll_inventory_verifications_locked(
    state: OtsState,
    staging_dir: Path,
    *,
    sp: SimpleProofClient,
) -> OtsState:
    """Poll outstanding inventory verify jobs and persist refreshed statuses."""
    state = poll_outstanding_jobs(state, sp)
    save_ots(staging_dir, state)
    return state


def snapshot_inventory_results_locked(
    state: OtsState,
    staging_dir: Path,
    *,
    sp: SimpleProofClient,
) -> OtsState:
    """Fetch mid-flight inventory result snapshots and persist per-job attestations."""
    return snapshot_results(state, staging_dir, sp)


def ingest_inventory_verifications_locked(
    state: OtsState,
    staging_dir: Path,
    *,
    sp: SimpleProofClient,
    btc: BitcoinRPC,
    min_confirmations: int,
    runtime: Runtime,
) -> OtsState:
    """Download completed inventory verify results and persist proof state."""
    return _process_completed_inventory_job(
        state, sp, btc, staging_dir, min_confirmations, runtime=runtime
    )


def upgrade_inventory_locked(
    cfg: Config,
    staging_dir: Path,
    *,
    sp_factory: SpClientFactory,
    btc_factory: BtcRpcFactory,
    runtime: Runtime,
) -> UpgradeInventoryResult:
    """One inventory `--upgrade` pass. Caller MUST hold the staging lock."""
    btc_cfg = cfg.bitcoin
    if btc_cfg is None or cfg.simpleproof is None:
        raise RuntimeError("config missing required sections")
    min_confs = btc_cfg.confirmations

    state = load_ots(staging_dir)
    prev_phase = state.phase
    check_transition(state.phase, "timestamp inventory --upgrade")
    category_id = apply_category_id(state, cfg.simpleproof.proof_category_id)

    if state.inventory_proof is None:
        raise PhaseError(f"inventory_proof missing in phase {state.phase.value!r}")

    sp = sp_factory(cfg)
    btc = btc_factory(cfg)

    state = prepare_inventory_upgrade_locked(
        state,
        staging_dir,
        sp=sp,
        btc=btc,
        category_id=category_id,
        min_confirmations=min_confs,
    )
    state = poll_inventory_verifications_locked(state, staging_dir, sp=sp)
    state = snapshot_inventory_results_locked(state, staging_dir, sp=sp)
    state = ingest_inventory_verifications_locked(
        state,
        staging_dir,
        sp=sp,
        btc=btc,
        min_confirmations=min_confs,
        runtime=runtime,
    )

    if (
        state.phase is Phase.INVENTORY_UPGRADED
        and prev_phase is not Phase.INVENTORY_UPGRADED
    ):
        log_phase(prev_phase, Phase.INVENTORY_UPGRADED)
    return UpgradeInventoryResult(
        previous_phase=prev_phase,
        phase=state.phase,
        job=compute_inventory_job_progress(state, runtime=runtime),
    )


def upgrade_inventory(
    cfg: Config,
    staging_dir: Path,
    *,
    sp_factory: SpClientFactory,
    btc_factory: BtcRpcFactory,
    runtime: Runtime,
) -> UpgradeInventoryResult:
    """Drive `stage timestamp inventory --upgrade` end-to-end under the lock."""
    with with_lock(staging_dir, timeout=runtime.lock_timeout):
        return upgrade_inventory_locked(
            cfg,
            staging_dir,
            sp_factory=sp_factory,
            btc_factory=btc_factory,
            runtime=runtime,
        )


def prepare_inventory_submission_locked(
    cfg: Config,
    store: StagingStore,
    sr,
    root_path: str,
    object_id: str,
    staging_dir: Path,
    *,
    allow_missing_metadata: bool,
) -> PreparedInventorySubmission:
    """Build inventory and persist its timestamp intent under the staging lock."""
    sp_cfg = cfg.simpleproof
    if sp_cfg is None:
        raise RuntimeError("simpleproof config missing")
    state = load_ots(staging_dir)
    target_phase = check_transition(state.phase, "timestamp inventory")

    # Hold the gate when a reorg (or any reverify) reset a content proof after
    # the phase advanced: the inventory must not be timestamped over an
    # unverified content proof. Re-anchor the content first.
    if state.phase is Phase.CONTENT_UPGRADED:
        require_verified_content_before_inventory(state)

    category_id = apply_category_id(state, sp_cfg.proof_category_id)

    if state.phase is Phase.INVENTORY_UPGRADED:
        return PreparedInventorySubmission(
            digest=None,
            category_id=category_id,
            previous_phase=state.phase,
            target_phase=state.phase,
        )

    metadata = store.load_metadata(root_path, object_id)
    require_staged_metadata(metadata, allow_missing_metadata=allow_missing_metadata)

    # On resubmit, preserve `created` from the prior plan so the new
    # inventory digest stays identical across retries.
    prior_created: str | None = None
    prior_plan = load_plan(staging_dir) if state.inventory_proof is not None else None
    if prior_plan is not None:
        prior_inv = prior_plan.inventory.inventory
        prior_created = prior_inv.versions[prior_inv.head].created

    plan, _cs = build_inventory_plan(
        store,
        sr,
        root_path,
        object_id,
        message=metadata.message,
        user=version_user_from_staged(metadata),
        created=prior_created,
    )
    inv_digest = plan.inventory.digest
    head_version = plan.version

    existing = state.inventory_proof
    intent_proof = InventoryProof(
        version=head_version,
        digest=inv_digest,
        proof=existing.proof if existing is not None else VerifiedProof(),
        job_id=existing.job_id if existing is not None else None,
    )
    prev_phase = state.phase
    save_ots(
        staging_dir,
        replace(
            state,
            category_id=category_id,
            inventory_proof=intent_proof,
            phase=target_phase,
        ),
    )

    return PreparedInventorySubmission(
        digest=inv_digest,
        category_id=category_id,
        previous_phase=prev_phase,
        target_phase=target_phase,
        plan=plan,
    )


def submit_prepared_inventory_locked(
    cfg: Config,
    staging_dir: Path,
    prepared: PreparedInventorySubmission,
    *,
    sp_factory: SpClientFactory,
) -> SubmitInventoryResult:
    """POST a prepared inventory digest and serialize its update plan."""
    if prepared.digest is None:
        return SubmitInventoryResult(
            submitted_count=0,
            previous_phase=prepared.previous_phase,
            phase=prepared.target_phase,
        )
    if prepared.plan is None:
        raise RuntimeError("prepared inventory submission missing serialized plan")

    sp = sp_factory(cfg)
    sp.submit_hashes([prepared.digest], prepared.category_id)

    plan_path(staging_dir).write_bytes(prepared.plan.to_json())

    log_phase(prepared.previous_phase, prepared.target_phase)
    return SubmitInventoryResult(
        submitted_count=1,
        previous_phase=prepared.previous_phase,
        phase=prepared.target_phase,
    )


def submit_inventory_locked(
    cfg: Config,
    store: StagingStore,
    sr,
    root_path: str,
    object_id: str,
    staging_dir: Path,
    *,
    sp_factory: SpClientFactory,
    allow_missing_metadata: bool,
) -> SubmitInventoryResult:
    """One `stage timestamp inventory` submit pass as a batch of size 1."""
    prepared = prepare_inventory_submission_locked(
        cfg,
        store,
        sr,
        root_path,
        object_id,
        staging_dir,
        allow_missing_metadata=allow_missing_metadata,
    )
    result = submit_prepared_inventory_locked(
        cfg, staging_dir, prepared, sp_factory=sp_factory
    )
    if prepared.digest is not None and result.submitted_count:
        logger.info("submitted inventory digest %s for %s", prepared.digest, object_id)
    return result


def retry_inventory_locked(
    cfg: Config,
    staging_dir: Path,
    object_id: str,
    *,
    sp_factory: SpClientFactory,
) -> None:
    """One `stage timestamp inventory --retry` pass under the lock."""
    sp_cfg = cfg.simpleproof
    if sp_cfg is None:
        raise RuntimeError("simpleproof config missing")
    state = load_ots(staging_dir)
    check_transition(state.phase, "timestamp inventory --retry")
    category_id = apply_category_id(state, sp_cfg.proof_category_id)
    if state.inventory_proof is None:
        raise InventoryProofMissingError("inventory_proof missing")
    inv_digest = state.inventory_proof.digest
    sp = sp_factory(cfg)
    sp.submit_hashes([inv_digest], category_id)
    logger.info("retried inventory submission for %s", object_id)


def submit_inventory(
    cfg: Config,
    store: StagingStore,
    sr,
    root_path: str,
    object_id: str,
    staging_dir: Path,
    *,
    sp_factory: SpClientFactory,
    allow_missing_metadata: bool,
    runtime: Runtime,
) -> SubmitInventoryResult:
    """`stage timestamp inventory`: build the plan, submit its digest, locked."""
    with with_lock(staging_dir, timeout=runtime.lock_timeout):
        return submit_inventory_locked(
            cfg,
            store,
            sr,
            root_path,
            object_id,
            staging_dir,
            sp_factory=sp_factory,
            allow_missing_metadata=allow_missing_metadata,
        )


def retry_inventory(
    cfg: Config,
    staging_dir: Path,
    object_id: str,
    *,
    sp_factory: SpClientFactory,
    runtime: Runtime,
) -> None:
    """`stage timestamp inventory --retry` under the staging lock."""
    with with_lock(staging_dir, timeout=runtime.lock_timeout):
        retry_inventory_locked(cfg, staging_dir, object_id, sp_factory=sp_factory)
