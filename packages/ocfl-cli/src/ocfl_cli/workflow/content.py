"""Content timestamping use cases: submit, upgrade, retry."""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from msgspec.structs import replace

from ocfl_cli.errors import (
    AttestationMismatchError,
)
from ocfl_cli.model.ots_state import (
    OtsState,
    Submission,
    VerifiedProof,
    VerifyJob,
)
from ocfl_cli.model.phase import Phase, check_transition
from ocfl_cli.ots.bitcoin import (
    PendingVerification,
    VerifiedAttestation,
    verify_attestation,
)
from ocfl_cli.store import staging as _store_mod
from ocfl_cli.store.ingest import ingest_file
from ocfl_cli.store.ots_state import (
    apply_category_id,
    load_ots,
    ots_cache_path,
    save_ots,
    utc_iso,
    with_lock,
)
from ocfl_cli.workflow.common import (
    JobProgress,
    StateMap,
    compute_job_progress,
    is_failed_verify_job,
    log_phase,
    non_ots_digests,
    poll_outstanding_jobs,
    record_proof,
    snapshot_results,
    unverified_digests,
    update_head_state,
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
    from ocfl_lib import DigestAlgorithm, Inventory

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SubmitContentResult:
    submitted_count: int
    previous_phase: Phase
    phase: Phase
    noop_advanced: bool = False


@dataclass(frozen=True, slots=True)
class PreparedContentSubmission:
    digests: tuple[str, ...]
    category_id: int
    previous_phase: Phase
    target_phase: Phase
    submission: Submission | None = None
    noop_advanced: bool = False


@dataclass(frozen=True, slots=True)
class UpgradeContentResult:
    previous_phase: Phase
    phase: Phase
    unverified_pending_count: int = 0
    orphan_count: int = 0
    jobs: tuple[JobProgress, ...] = ()


def _covered_digests(state: OtsState) -> set[str]:
    """Digests already covered by a non-FAILED verify_job."""
    return {
        d
        for j in state.verify_jobs
        if not is_failed_verify_job(j.status)
        for d in j.digests
    }


def reverify_content_proofs(
    state: OtsState,
    btc: BitcoinRPC,
    sp: SimpleProofClient,
    staging_dir: Path,
    min_confirmations: int,
    category_id: int,
) -> OtsState:
    """Re-verify every `verified=True` content_proof against the local chain.

    Uses cached `.ots` bytes (which embed attested block_height + merkle
    path); `verify_attestation` resolves the current block_hash at that
    height via `getblockhash` and checks the merkle root. On mismatch or
    insufficient-confs the proof resets to `verified=False` and the
    covering VerifyJobs are dropped, so a fresh `verify-batches` is POSTed
    and the next pass re-fetches bytes from SP. `ReorgDetectedError`
    propagates to HALT.
    """
    new_proofs = dict(state.content_proofs)
    needs_resubmit: list[str] = []
    for d, proof in state.content_proofs.items():
        if not proof.verified:
            continue
        cache = ots_cache_path(staging_dir, d)
        if not cache.is_file():
            continue
        try:
            result = verify_attestation(
                rpc=btc,
                ots_bytes=cache.read_bytes(),
                expected_digest=bytes.fromhex(d),
                min_confirmations=min_confirmations,
            )
        except AttestationMismatchError:
            new_proofs[d] = VerifiedProof(ots_path=proof.ots_path, verified=False)
            needs_resubmit.append(d)
            continue
        match result:
            case VerifiedAttestation(block_height=h, confirmations=c):
                new_proofs[d] = VerifiedProof(
                    ots_path=proof.ots_path,
                    verified=True,
                    block_height=h,
                    block_hash=result.block_hash,
                    confirmations_at_verify=c,
                    verified_at=proof.verified_at,
                )
            case PendingVerification():
                new_proofs[d] = VerifiedProof(ots_path=proof.ots_path, verified=False)
                needs_resubmit.append(d)

    if not needs_resubmit:
        return replace(state, content_proofs=new_proofs)

    affected = set(needs_resubmit)
    kept_jobs = tuple(j for j in state.verify_jobs if affected.isdisjoint(j.digests))
    new_job = verify_job_from_ack(
        sp.verify_batch(list(needs_resubmit), category_id),
        tuple(needs_resubmit),
    )
    return replace(
        state,
        content_proofs=new_proofs,
        verify_jobs=(*kept_jobs, new_job),
    )


def _submit_new_verify_batches(
    state: OtsState, sp: SimpleProofClient, category_id: int
) -> OtsState:
    """POST `verify-batches` for every submitted digest not already covered."""
    covered = _covered_digests(state)
    new_jobs: list[VerifyJob] = []
    for sub in state.submissions:
        if sub.status != "submitted":
            continue
        uncovered = tuple(d for d in sub.digests if d not in covered)
        if not uncovered:
            continue
        new_jobs.append(
            verify_job_from_ack(
                sp.verify_batch(list(uncovered), category_id),
                uncovered,
            )
        )
        covered.update(uncovered)
    if not new_jobs:
        return state
    return replace(state, verify_jobs=(*state.verify_jobs, *new_jobs))


def verify_and_ingest_digest(
    d: str,
    ots_bytes: bytes,
    state: OtsState,
    inv: Inventory,
    head_state: StateMap,
    alg: DigestAlgorithm,
    *,
    btc: BitcoinRPC,
    store: StagingStore,
    root_path: str,
    object_id: str,
    staging_dir: Path,
    min_confirmations: int,
    runtime: Runtime,
) -> tuple[OtsState, Inventory]:
    """Apply the three-step crash-recovery invariant for one digest.

    Ordering (must not change):
      1. atomic cache write `ots-cache/<d>.ots`,
      2. `ingest_file` the cached bytes as `<obj_rel>.ots` (stage-state mutation),
      3. atomic `ots.json` update recording VerifiedProof.

    On `PendingVerification` only step (1) runs plus a `verified=False`
    proof record; the next pass re-downloads and re-verifies. `head_state`
    is mutated in place to track accumulated ingests across digests.
    """
    result = verify_attestation(
        rpc=btc,
        ots_bytes=ots_bytes,
        expected_digest=bytes.fromhex(d),
        min_confirmations=min_confirmations,
    )

    cache_file = ots_cache_path(staging_dir, d)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    _store_mod.atomic_write_bytes(cache_file, ots_bytes)
    ots_path_rel = f"ots-cache/{d}.ots"

    if not isinstance(result, VerifiedAttestation):
        state = record_proof(
            state, d, VerifiedProof(ots_path=ots_path_rel, verified=False)
        )
        save_ots(staging_dir, state)
        return state, inv

    paths = head_state.get(d, [])
    if not paths:
        return state, inv
    logical = f"{paths[0]}.ots"

    content_map = store.load_content_map(root_path, object_id)
    ots_digest = ingest_file(
        cache_file,
        logical,
        staging_dir=staging_dir,
        content_map=content_map,
        digest_alg=alg,
    ).digest
    logicals = head_state.setdefault(ots_digest, [])
    if logical not in logicals:
        logicals.append(logical)
    store.save_inventory(root_path, object_id, update_head_state(inv, head_state))
    store.save_content_map(root_path, object_id, content_map)
    inv = store.load_inventory(root_path, object_id)

    state = record_proof(
        state,
        d,
        VerifiedProof(
            ots_path=ots_path_rel,
            verified=True,
            block_height=result.block_height,
            block_hash=result.block_hash,
            confirmations_at_verify=result.confirmations,
            verified_at=utc_iso(runtime.clock.now()),
        ),
    )
    save_ots(staging_dir, state)
    return state, inv


def _process_completed_jobs(
    state: OtsState,
    sp: SimpleProofClient,
    btc: BitcoinRPC,
    store: StagingStore,
    root_path: str,
    object_id: str,
    staging_dir: Path,
    min_confirmations: int,
    *,
    runtime: Runtime,
) -> OtsState:
    """Download + verify + ingest every completed verify_job's digests."""
    from ocfl_lib import DigestAlgorithm

    inv = store.load_inventory(root_path, object_id)
    alg = DigestAlgorithm.from_ocfl_name(inv.digest_algorithm)
    head_state: StateMap = {
        d: list(ps) for d, ps in inv.versions[inv.head].state.items()
    }

    for j in state.verify_jobs:
        if j.status != "COMPLETED":
            continue
        unverified = unverified_digests(state, j.digests)
        if not unverified:
            continue
        poll = sp.poll_job(j.job_id)
        if poll.results_url is None:
            continue
        downloads = sp.download_results(poll.results_url, set(unverified))
        for d, ots_bytes in downloads.items():
            state, inv = verify_and_ingest_digest(
                d,
                ots_bytes,
                state,
                inv,
                head_state,
                alg,
                btc=btc,
                store=store,
                root_path=root_path,
                object_id=object_id,
                staging_dir=staging_dir,
                min_confirmations=min_confirmations,
                runtime=runtime,
            )
    return state


def phase_advance_ready(
    state: OtsState,
    store: StagingStore,
    sr,
    root_path: str,
    object_id: str,
) -> tuple[bool, list[str], set[str]]:
    """Evaluate the phase-advance invariant.

    Returns `(ready, unverified_pending, orphans)`:
      - cond1 (all pending verified): `unverified_pending` is empty
      - cond2 (no orphan non-.ots digests outside pending): `orphans` is empty
      - `ready` iff both conditions hold
    """
    final_inv = store.load_inventory(root_path, object_id)
    final_state = {
        d: list(ps) for d, ps in final_inv.versions[final_inv.head].state.items()
    }
    obj = sr.get_object(object_id)
    carry = set(obj.inventory.inventory.manifest) if obj.exists else set()
    unverified_pending = unverified_digests(state, state.pending_digests)
    orphans = (non_ots_digests(final_state) - carry) - set(state.pending_digests)
    return (not unverified_pending and not orphans, unverified_pending, orphans)


def upgrade_content_locked(
    cfg: Config,
    store: StagingStore,
    sr,
    root_path: str,
    object_id: str,
    staging_dir: Path,
    *,
    sp_factory: SpClientFactory,
    btc_factory: BtcRpcFactory,
    runtime: Runtime,
) -> UpgradeContentResult:
    """One `--upgrade` pass. Caller MUST hold the staging lock."""
    btc_cfg = cfg.bitcoin
    if btc_cfg is None or cfg.simpleproof is None:
        raise RuntimeError("config missing required sections")
    min_confs = btc_cfg.confirmations

    state = load_ots(staging_dir)
    prev_phase = state.phase
    check_transition(state.phase, "timestamp content --upgrade")
    category_id = apply_category_id(state, cfg.simpleproof.proof_category_id)

    sp = sp_factory(cfg)
    btc = btc_factory(cfg)

    state = prepare_content_upgrade_locked(
        state,
        staging_dir,
        sp=sp,
        btc=btc,
        category_id=category_id,
        min_confirmations=min_confs,
    )
    state = poll_content_verifications_locked(state, staging_dir, sp=sp)
    state = snapshot_content_results_locked(state, staging_dir, sp=sp)

    state = ingest_content_verifications_locked(
        state,
        store,
        root_path,
        object_id,
        staging_dir,
        sp=sp,
        btc=btc,
        min_confirmations=min_confs,
        runtime=runtime,
    )

    ready, unverified_pending, orphans = phase_advance_ready(
        state, store, sr, root_path, object_id
    )
    if ready and state.phase is not Phase.CONTENT_UPGRADED:
        state = replace(state, phase=Phase.CONTENT_UPGRADED)
        save_ots(staging_dir, state)
        log_phase(prev_phase, Phase.CONTENT_UPGRADED)
        return UpgradeContentResult(
            previous_phase=prev_phase,
            phase=state.phase,
            jobs=compute_job_progress(state, runtime=runtime),
        )
    if unverified_pending:
        logger.debug(
            "phase advance held: %d pending digest(s) not yet verified",
            len(unverified_pending),
        )
    if orphans:
        logger.debug(
            "phase advance held: %d orphan digest(s) outside pending set",
            len(orphans),
        )
    return UpgradeContentResult(
        previous_phase=prev_phase,
        phase=state.phase,
        unverified_pending_count=len(unverified_pending),
        orphan_count=len(orphans),
        jobs=compute_job_progress(state, runtime=runtime),
    )


def prepare_content_upgrade_locked(
    state: OtsState,
    staging_dir: Path,
    *,
    sp: SimpleProofClient,
    btc: BitcoinRPC,
    category_id: int,
    min_confirmations: int,
) -> OtsState:
    """Reverify cached proofs and submit missing verify jobs for content."""
    state = reverify_content_proofs(
        state, btc, sp, staging_dir, min_confirmations, category_id
    )
    save_ots(staging_dir, state)

    state = _submit_new_verify_batches(state, sp, category_id)
    save_ots(staging_dir, state)
    return state


def poll_content_verifications_locked(
    state: OtsState,
    staging_dir: Path,
    *,
    sp: SimpleProofClient,
) -> OtsState:
    """Poll outstanding content verify jobs and persist refreshed statuses."""
    state = poll_outstanding_jobs(state, sp)
    save_ots(staging_dir, state)
    return state


def snapshot_content_results_locked(
    state: OtsState,
    staging_dir: Path,
    *,
    sp: SimpleProofClient,
) -> OtsState:
    """Fetch mid-flight content result snapshots and persist per-job attestations."""
    return snapshot_results(state, staging_dir, sp)


def ingest_content_verifications_locked(
    state: OtsState,
    store: StagingStore,
    root_path: str,
    object_id: str,
    staging_dir: Path,
    *,
    sp: SimpleProofClient,
    btc: BitcoinRPC,
    min_confirmations: int,
    runtime: Runtime,
) -> OtsState:
    """Download completed content verify results and ingest verified proofs."""
    return _process_completed_jobs(
        state,
        sp,
        btc,
        store,
        root_path,
        object_id,
        staging_dir,
        min_confirmations,
        runtime=runtime,
    )


def upgrade_content(
    cfg: Config,
    store: StagingStore,
    sr,
    root_path: str,
    object_id: str,
    staging_dir: Path,
    *,
    sp_factory: SpClientFactory,
    btc_factory: BtcRpcFactory,
    runtime: Runtime,
) -> UpgradeContentResult:
    """Drive `stage timestamp content --upgrade` end-to-end under the lock."""
    with with_lock(staging_dir, timeout=runtime.lock_timeout):
        return upgrade_content_locked(
            cfg,
            store,
            sr,
            root_path,
            object_id,
            staging_dir,
            sp_factory=sp_factory,
            btc_factory=btc_factory,
            runtime=runtime,
        )


def prepare_content_submission_locked(
    cfg: Config,
    store: StagingStore,
    sr,
    root_path: str,
    object_id: str,
    staging_dir: Path,
    *,
    runtime: Runtime,
) -> PreparedContentSubmission:
    """Prepare a durable content submission intent under the staging lock."""
    sp_cfg = cfg.simpleproof
    if sp_cfg is None:
        raise RuntimeError("simpleproof config missing")
    state = load_ots(staging_dir)
    target_phase = check_transition(state.phase, "timestamp content")
    category_id = apply_category_id(state, sp_cfg.proof_category_id)

    inv = store.load_inventory(root_path, object_id)
    head_state = inv.versions[inv.head].state
    non_ots = {
        d
        for d, paths in head_state.items()
        if not any(p.endswith(".ots") for p in paths)
    }

    obj = sr.get_object(object_id)
    carry = set(obj.inventory.inventory.manifest) if obj.exists else set()

    in_flight = [s for s in state.submissions if s.status == "submitting"]
    submitting_digests = {d for s in in_flight for d in s.digests}
    max_attempts = max((s.attempts for s in in_flight), default=0)

    already_handled = set(state.pending_digests) | set(state.content_proofs)
    submission_set = (non_ots - carry - already_handled) | submitting_digests

    if not submission_set:
        if not state.pending_digests and non_ots <= carry:
            save_ots(staging_dir, replace(state, phase=Phase.CONTENT_UPGRADED))
            log_phase(state.phase, Phase.CONTENT_UPGRADED)
            return PreparedContentSubmission(
                digests=(),
                category_id=category_id,
                previous_phase=state.phase,
                target_phase=Phase.CONTENT_UPGRADED,
                noop_advanced=True,
            )
        if target_phase is not state.phase:
            save_ots(staging_dir, replace(state, phase=target_phase))
            log_phase(state.phase, target_phase)
        return PreparedContentSubmission(
            digests=(),
            category_id=category_id,
            previous_phase=state.phase,
            target_phase=target_phase,
        )

    ordered = tuple(sorted(submission_set))
    kept = tuple(s for s in state.submissions if s.status != "submitting")
    merged_pending = tuple(dict.fromkeys((*state.pending_digests, *ordered)))

    intent = Submission(
        submitted_at=utc_iso(runtime.clock.now()),
        digests=ordered,
        status="submitting",
        attempts=max_attempts + 1,
    )
    save_ots(
        staging_dir,
        replace(
            state,
            category_id=category_id,
            submissions=(*kept, intent),
            pending_digests=merged_pending,
        ),
    )
    return PreparedContentSubmission(
        digests=ordered,
        category_id=category_id,
        previous_phase=state.phase,
        target_phase=target_phase,
        submission=intent,
    )


def submit_prepared_content_locked(
    cfg: Config,
    staging_dir: Path,
    prepared: PreparedContentSubmission,
    *,
    sp_factory: SpClientFactory,
) -> SubmitContentResult:
    """POST prepared content digests and finalize their durable submission."""
    if not prepared.digests:
        return SubmitContentResult(
            submitted_count=0,
            previous_phase=prepared.previous_phase,
            phase=prepared.target_phase,
            noop_advanced=prepared.noop_advanced,
        )
    if prepared.submission is None:
        raise RuntimeError("prepared content submission missing durable intent")

    sp = sp_factory(cfg)
    size = sp.submit_chunk_size
    for i in range(0, len(prepared.digests), size):
        sp.submit_hashes(
            list(prepared.digests[i : i + size]),
            prepared.category_id,
        )

    state = load_ots(staging_dir)
    kept = tuple(s for s in state.submissions if s.status != "submitting")
    submitted = replace(prepared.submission, status="submitted")
    save_ots(
        staging_dir,
        replace(
            state,
            category_id=prepared.category_id,
            submissions=(*kept, submitted),
            phase=prepared.target_phase,
        ),
    )

    log_phase(prepared.previous_phase, prepared.target_phase)
    return SubmitContentResult(
        submitted_count=len(prepared.digests),
        previous_phase=prepared.previous_phase,
        phase=prepared.target_phase,
    )


def submit_content_locked(
    cfg: Config,
    store: StagingStore,
    sr,
    root_path: str,
    object_id: str,
    staging_dir: Path,
    *,
    sp_factory: SpClientFactory,
    runtime: Runtime,
) -> SubmitContentResult:
    """One `stage timestamp content` submit pass as a batch of size 1."""
    prepared = prepare_content_submission_locked(
        cfg, store, sr, root_path, object_id, staging_dir, runtime=runtime
    )
    result = submit_prepared_content_locked(
        cfg, staging_dir, prepared, sp_factory=sp_factory
    )
    if result.submitted_count:
        logger.info("submitted %d digests for %s", result.submitted_count, object_id)
    return result


class NoFailedSubmissionsError(Exception):
    """`--retry` invoked but no submissions are in `failed` state."""


def retry_content_locked(
    cfg: Config,
    staging_dir: Path,
    object_id: str,
    *,
    sp_factory: SpClientFactory,
    runtime: Runtime,
) -> None:
    """One `stage timestamp content --retry` pass under the lock."""
    sp_cfg = cfg.simpleproof
    if sp_cfg is None:
        raise RuntimeError("simpleproof config missing")
    state = load_ots(staging_dir)
    target_phase = check_transition(state.phase, "timestamp content --retry")
    category_id = apply_category_id(state, sp_cfg.proof_category_id)

    failed_indices = [
        i for i, s in enumerate(state.submissions) if s.status == "failed"
    ]
    if not failed_indices:
        raise NoFailedSubmissionsError("no failed submissions to retry")

    # Crash-safe write protocol: intent (submitting, attempts++) ->
    # POST -> committed (submitted). Index-based mutation avoids
    # mis-targeting when two submissions share identical digests.
    subs = list(state.submissions)
    for i in failed_indices:
        subs[i] = replace(
            subs[i],
            submitted_at=utc_iso(runtime.clock.now()),
            status="submitting",
            attempts=subs[i].attempts + 1,
        )
    save_ots(
        staging_dir,
        replace(state, category_id=category_id, submissions=tuple(subs)),
    )

    sp = sp_factory(cfg)
    for i in failed_indices:
        digests = list(subs[i].digests)
        size = sp.submit_chunk_size
        for j in range(0, len(digests), size):
            sp.submit_hashes(digests[j : j + size], category_id)

    for i in failed_indices:
        subs[i] = replace(subs[i], status="submitted")
    save_ots(
        staging_dir,
        replace(
            state,
            category_id=category_id,
            submissions=tuple(subs),
            phase=target_phase,
        ),
    )

    logger.info(
        "retried %d failed submission(s) for %s",
        len(failed_indices),
        object_id,
    )


def submit_content(
    cfg: Config,
    store: StagingStore,
    sr,
    root_path: str,
    object_id: str,
    staging_dir: Path,
    *,
    sp_factory: SpClientFactory,
    runtime: Runtime,
) -> SubmitContentResult:
    """`stage timestamp content`: submit the content digests under the lock."""
    with with_lock(staging_dir, timeout=runtime.lock_timeout):
        return submit_content_locked(
            cfg,
            store,
            sr,
            root_path,
            object_id,
            staging_dir,
            sp_factory=sp_factory,
            runtime=runtime,
        )


def retry_content(
    cfg: Config,
    staging_dir: Path,
    object_id: str,
    *,
    sp_factory: SpClientFactory,
    runtime: Runtime,
) -> None:
    """`stage timestamp content --retry` under the staging lock."""
    with with_lock(staging_dir, timeout=runtime.lock_timeout):
        retry_content_locked(
            cfg, staging_dir, object_id, sp_factory=sp_factory, runtime=runtime
        )
