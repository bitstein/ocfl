import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from msgspec.structs import replace

from ocfl_cli.errors import AttestationMismatchError, PhaseError
from ocfl_cli.model.ots_state import OtsState, Proof, Submission
from ocfl_cli.model.phase import (
    Phase,
    PhaseCommand,
    TimestampTarget,
    TimestampVerb,
    check_transition,
    is_legal,
    target_for,
)
from ocfl_cli.model.simpleproof import RESUBMIT_VERIFY_STATUSES, VerifyJobStatus
from ocfl_cli.ots.bitcoin import (
    PendingVerification,
    VerifiedAttestation,
    verify_attestation,
)
from ocfl_cli.store import staging as _store_mod
from ocfl_cli.store.ots_state import (
    load_ots,
    ots_cache_path,
    persisted_category_id,
    save_ots,
    utc_iso,
    with_lock,
)
from ocfl_cli.workflow import status as wf_status
from ocfl_cli.workflow import targets
from ocfl_cli.workflow.common import (
    JobProgress,
    compute_job_progress,
    log_phase,
    poll_outstanding_jobs,
    record_proof,
    request_proofs,
    snapshot_results,
    unverify,
    verified_proof,
    verify_job_from_ack,
)
from ocfl_cli.workflow.targets import Area, TargetRules, rules_for

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
    from ocfl_cli.store.ots_state import LockedStaging
    from ocfl_cli.store.staging import StagingStore
    from ocfl_lib import StorageRoot

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TimestampRun:
    cfg: Config
    store: StagingStore
    sr: StorageRoot
    root_path: str
    object_id: str
    staging_dir: Path
    sp_factory: SpClientFactory
    btc_factory: BtcRpcFactory
    runtime: Runtime

    @property
    def area(self) -> Area:
        return Area(
            store=self.store,
            sr=self.sr,
            root_path=self.root_path,
            object_id=self.object_id,
            staging_dir=self.staging_dir,
        )

    @property
    def category_config(self) -> int:
        if self.cfg.simpleproof is None:
            raise RuntimeError("simpleproof config missing")
        return self.cfg.simpleproof.proof_category_id

    @property
    def min_confirmations(self) -> int:
        if self.cfg.bitcoin is None:
            raise RuntimeError("bitcoin config missing")
        return self.cfg.bitcoin.confirmations


@dataclass(frozen=True, slots=True)
class PreparedSubmission:
    target: TimestampTarget
    digests: tuple[str, ...]
    category_id: int
    previous_phase: Phase
    target_phase: Phase
    submission: Submission | None = None
    noop_advanced: bool = False


def _done(target: TimestampTarget, phase: Phase) -> bool:
    return rules_for(target).upgraded_phase is phase


@dataclass(frozen=True, slots=True)
class SubmitResult:
    target: TimestampTarget
    submitted_count: int
    previous_phase: Phase
    phase: Phase
    noop_advanced: bool = False

    @property
    def done(self) -> bool:
        return _done(self.target, self.phase)


@dataclass(frozen=True, slots=True)
class UpgradeResult:
    target: TimestampTarget
    previous_phase: Phase
    phase: Phase
    unverified_pending_count: int = 0
    orphan_count: int = 0
    jobs: tuple[JobProgress, ...] = ()

    @property
    def done(self) -> bool:
        return _done(self.target, self.phase)


@dataclass(frozen=True, slots=True)
class RetryResult:
    target: TimestampTarget
    retried: int
    phase: Phase

    @property
    def done(self) -> bool:
        return _done(self.target, self.phase)


@dataclass(frozen=True, slots=True)
class Idle:
    target: TimestampTarget
    phase: Phase

    @property
    def done(self) -> bool:
        return _done(self.target, self.phase)


def idle(
    run: TimestampRun, verb: TimestampVerb, *, expect: TimestampTarget | None = None
) -> Idle | None:
    if expect is not None:
        return None
    state = load_ots(run.staging_dir)
    rules = resolve(state, None)
    if is_legal(state.phase, PhaseCommand.targeted(verb, rules.target)):
        return None
    shown = next((r for r in targets.ALL if r.upgraded_phase is state.phase), rules)
    return Idle(target=shown.target, phase=state.phase)


def check(
    run: TimestampRun, verb: TimestampVerb, *, expect: TimestampTarget | None = None
) -> TimestampTarget:
    state = load_ots(run.staging_dir)
    rules = resolve(state, expect)
    check_transition(state.phase, PhaseCommand.targeted(verb, rules.target))
    return rules.target


def next_step(run: TimestampRun) -> str:
    return wf_status.next_step(run.store, run.root_path, run.object_id, run.staging_dir)


def resolve(state: OtsState, expect: TimestampTarget | None) -> TargetRules:
    target = expect if expect is not None else target_for(state.phase)
    if target is None:
        raise PhaseError(f"nothing to timestamp in phase {state.phase.value!r}")
    return rules_for(target)


def target_jobs(
    state: OtsState, rules: TargetRules, *, runtime: Runtime
) -> tuple[JobProgress, ...]:
    mine = {
        j.job_id
        for j in state.verify_jobs
        if any(
            (p := state.proofs.get(d)) is not None and p.target is rules.target
            for d in j.digests
        )
    }
    return tuple(
        j for j in compute_job_progress(state, runtime=runtime) if j.job_id in mine
    )


def prepare_submission_locked(
    area: Area,
    staging: LockedStaging,
    *,
    category_config: int,
    runtime: Runtime,
    expect: TimestampTarget | None = None,
    allow_missing_metadata: bool = False,
) -> PreparedSubmission:
    """Send only digests with no proof, requested or received (ADR-0004)."""
    state = load_ots(staging)
    rules = resolve(state, expect)
    target_phase = check_transition(
        state.phase, PhaseCommand.targeted("timestamp", rules.target)
    )
    category_id = persisted_category_id(state, category_config)
    previous = state.phase
    if previous is rules.upgraded_phase:
        return PreparedSubmission(
            target=rules.target,
            digests=(),
            category_id=category_id,
            previous_phase=previous,
            target_phase=previous,
            noop_advanced=True,
        )

    state, needed = rules.prepare(
        area, state, allow_missing_metadata=allow_missing_metadata
    )
    in_flight = [
        s
        for s in state.submissions
        if s.status == "submitting" and s.target is rules.target
    ]
    to_send = {d for d in needed if d not in state.proofs} | {
        d for s in in_flight for d in s.digests
    }

    if not to_send:
        done = rules.progress(area, state).done
        phase = rules.upgraded_phase if done else target_phase
        save_ots(staging, replace(state, category_id=category_id, phase=phase))
        log_phase(previous, phase)
        return PreparedSubmission(
            target=rules.target,
            digests=(),
            category_id=category_id,
            previous_phase=previous,
            target_phase=phase,
            noop_advanced=done,
        )

    ordered = tuple(sorted(to_send))
    intent = Submission(
        submitted_at=utc_iso(runtime.clock.now()),
        digests=ordered,
        status="submitting",
        target=rules.target,
        attempts=max((s.attempts for s in in_flight), default=0) + 1,
    )
    kept = tuple(s for s in state.submissions if s not in in_flight)
    save_ots(
        staging,
        replace(
            request_proofs(state, ordered, rules.target),
            category_id=category_id,
            submissions=(*kept, intent),
        ),
    )
    return PreparedSubmission(
        target=rules.target,
        digests=ordered,
        category_id=category_id,
        previous_phase=previous,
        target_phase=target_phase,
        submission=intent,
    )


def submit_prepared_locked(
    run: TimestampRun, staging: LockedStaging, prepared: PreparedSubmission
) -> SubmitResult:
    if not prepared.digests:
        return SubmitResult(
            target=prepared.target,
            submitted_count=0,
            previous_phase=prepared.previous_phase,
            phase=prepared.target_phase,
            noop_advanced=prepared.noop_advanced,
        )
    if prepared.submission is None:
        raise RuntimeError("prepared submission missing its durable intent")

    sp = run.sp_factory(run.cfg)
    size = sp.submit_chunk_size
    for i in range(0, len(prepared.digests), size):
        sp.submit_hashes(list(prepared.digests[i : i + size]), prepared.category_id)

    state = load_ots(staging)
    submitted = replace(prepared.submission, status="submitted")
    subs = tuple(
        submitted if s == prepared.submission else s for s in state.submissions
    )
    save_ots(
        staging,
        replace(
            state,
            category_id=prepared.category_id,
            submissions=subs,
            phase=prepared.target_phase,
        ),
    )
    log_phase(prepared.previous_phase, prepared.target_phase)
    logger.info(
        "submitted %d %s digest(s) for %s",
        len(prepared.digests),
        prepared.target.value,
        run.object_id,
    )
    return SubmitResult(
        target=prepared.target,
        submitted_count=len(prepared.digests),
        previous_phase=prepared.previous_phase,
        phase=prepared.target_phase,
    )


def submit(
    run: TimestampRun,
    *,
    expect: TimestampTarget | None = None,
    allow_missing_metadata: bool = False,
) -> SubmitResult:
    with with_lock(run.staging_dir, run.runtime) as staging:
        prepared = prepare_submission_locked(
            run.area,
            staging,
            category_config=run.category_config,
            runtime=run.runtime,
            expect=expect,
            allow_missing_metadata=allow_missing_metadata,
        )
        return submit_prepared_locked(run, staging, prepared)


def verify_digest(
    d: str,
    ots_bytes: bytes,
    state: OtsState,
    target: TimestampTarget,
    *,
    btc: BitcoinRPC,
    staging_dir: Path,
    min_confirmations: int,
    runtime: Runtime,
) -> OtsState:
    result = verify_attestation(
        rpc=btc,
        ots_bytes=ots_bytes,
        expected_digest=bytes.fromhex(d),
        min_confirmations=min_confirmations,
    )
    cache_file = ots_cache_path(staging_dir, d)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    _store_mod.atomic_write_bytes(cache_file, ots_bytes)
    ots_path = f"ots-cache/{d}.ots"
    if isinstance(result, VerifiedAttestation):
        proof = verified_proof(target, ots_path, result, utc_iso(runtime.clock.now()))
    else:
        proof = Proof(target=target, ots_path=ots_path)
    state = record_proof(state, d, proof)
    save_ots(staging_dir, state)
    return state


def reverify(
    state: OtsState,
    digests: tuple[str, ...],
    *,
    btc: BitcoinRPC,
    sp: SimpleProofClient,
    staging_dir: Path,
    min_confirmations: int,
    category_id: int,
) -> OtsState:
    proofs = dict(state.proofs)
    reset: list[str] = []
    for d in digests:
        proof = proofs.get(d)
        cache = ots_cache_path(staging_dir, d)
        if proof is None or not proof.verified or not cache.is_file():
            continue
        try:
            result = verify_attestation(
                rpc=btc,
                ots_bytes=cache.read_bytes(),
                expected_digest=bytes.fromhex(d),
                min_confirmations=min_confirmations,
            )
        except AttestationMismatchError:
            proofs[d] = unverify(proof)
            reset.append(d)
            continue
        match result:
            case VerifiedAttestation():
                proofs[d] = verified_proof(
                    proof.target, proof.ots_path, result, proof.verified_at
                )
            case PendingVerification():
                proofs[d] = unverify(proof)
                reset.append(d)
    state = replace(state, proofs=proofs)
    if not reset:
        return state
    kept = tuple(j for j in state.verify_jobs if set(reset).isdisjoint(j.digests))
    job = verify_job_from_ack(sp.verify_batch(reset, category_id), tuple(reset))
    return replace(state, verify_jobs=(*kept, job))


def _queue_verify_jobs(
    state: OtsState, rules: TargetRules, sp: SimpleProofClient, category_id: int
) -> OtsState:
    covered = {
        d
        for j in state.verify_jobs
        if j.status not in RESUBMIT_VERIFY_STATUSES
        for d in j.digests
    }
    jobs = list(state.verify_jobs)
    for sub in state.submissions:
        if sub.status != "submitted" or sub.target is not rules.target:
            continue
        open_digests = tuple(
            d
            for d in sub.digests
            if d not in covered and not state.verified(d) and rules.in_scope(state, d)
        )
        if not open_digests:
            continue
        jobs.append(
            verify_job_from_ack(
                sp.verify_batch(list(open_digests), category_id), open_digests
            )
        )
        covered.update(open_digests)
    if len(jobs) == len(state.verify_jobs):
        return state
    return replace(state, verify_jobs=tuple(jobs))


def _ingest_completed_jobs(
    run: TimestampRun,
    staging: LockedStaging,
    state: OtsState,
    rules: TargetRules,
    *,
    sp: SimpleProofClient,
    btc: BitcoinRPC,
) -> OtsState:
    for job in state.verify_jobs:
        if job.status != VerifyJobStatus.COMPLETED:
            continue
        wanted = {
            d for d in job.digests if not state.verified(d) and rules.in_scope(state, d)
        }
        if not wanted:
            continue
        poll = sp.poll_job(job.job_id)
        if poll.results_url is None:
            continue
        for d, ots_bytes in sp.download_results(poll.results_url, wanted).items():
            if d not in wanted:
                continue
            state = verify_digest(
                d,
                ots_bytes,
                state,
                rules.target,
                btc=btc,
                staging_dir=staging,
                min_confirmations=run.min_confirmations,
                runtime=run.runtime,
            )
    return state


def upgrade_locked(
    run: TimestampRun, staging: LockedStaging, *, expect: TimestampTarget | None = None
) -> UpgradeResult:
    state = load_ots(staging)
    rules = resolve(state, expect)
    previous = state.phase
    check_transition(state.phase, PhaseCommand.targeted("upgrade", rules.target))
    category_id = persisted_category_id(state, run.category_config)
    rules.check_upgradable(state)
    min_confs = run.min_confirmations

    sp = run.sp_factory(run.cfg)
    btc = run.btc_factory(run.cfg)

    state = reverify(
        state,
        rules.reverify_digests(state),
        btc=btc,
        sp=sp,
        staging_dir=staging,
        min_confirmations=min_confs,
        category_id=category_id,
    )
    save_ots(staging, state)
    state = _queue_verify_jobs(state, rules, sp, category_id)
    save_ots(staging, state)
    state = poll_outstanding_jobs(state, sp)
    save_ots(staging, state)
    state = snapshot_results(state, staging, sp)
    state = _ingest_completed_jobs(run, staging, state, rules, sp=sp, btc=btc)

    progress = rules.progress(run.area, state)
    if progress.done and state.phase is not rules.upgraded_phase:
        state = replace(state, phase=rules.upgraded_phase)
        save_ots(staging, state)
        log_phase(previous, state.phase)
    if progress.unverified:
        logger.debug(
            "phase advance held: %d digest(s) not yet verified",
            len(progress.unverified),
        )
    if progress.unrequested:
        logger.debug(
            "phase advance held: %d digest(s) not yet submitted",
            len(progress.unrequested),
        )
    return UpgradeResult(
        target=rules.target,
        previous_phase=previous,
        phase=state.phase,
        unverified_pending_count=0 if progress.done else len(progress.unverified),
        orphan_count=0 if progress.done else len(progress.unrequested),
        jobs=target_jobs(state, rules, runtime=run.runtime),
    )


def upgrade(
    run: TimestampRun, *, expect: TimestampTarget | None = None
) -> UpgradeResult:
    with with_lock(run.staging_dir, run.runtime) as staging:
        return upgrade_locked(run, staging, expect=expect)


def retry_locked(
    run: TimestampRun, staging: LockedStaging, *, expect: TimestampTarget | None = None
) -> RetryResult:
    state = load_ots(staging)
    rules = resolve(state, expect)
    target_phase = check_transition(
        state.phase, PhaseCommand.targeted("retry", rules.target)
    )
    category_id = persisted_category_id(state, run.category_config)
    failed = [
        i
        for i, s in enumerate(state.submissions)
        if s.status == "failed" and s.target is rules.target
    ]
    if not failed:
        return RetryResult(target=rules.target, retried=0, phase=state.phase)

    subs = list(state.submissions)
    for i in failed:
        subs[i] = replace(
            subs[i],
            submitted_at=utc_iso(run.runtime.clock.now()),
            status="submitting",
            attempts=subs[i].attempts + 1,
        )
    save_ots(
        staging,
        replace(state, category_id=category_id, submissions=tuple(subs)),
    )
    sp = run.sp_factory(run.cfg)
    size = sp.submit_chunk_size
    for i in failed:
        digests = list(subs[i].digests)
        for j in range(0, len(digests), size):
            sp.submit_hashes(digests[j : j + size], category_id)
    for i in failed:
        subs[i] = replace(subs[i], status="submitted")
    save_ots(
        staging,
        replace(
            state,
            category_id=category_id,
            submissions=tuple(subs),
            phase=target_phase,
        ),
    )
    logger.info("retried %d failed submission(s) for %s", len(failed), run.object_id)
    return RetryResult(target=rules.target, retried=len(failed), phase=target_phase)


def retry(run: TimestampRun, *, expect: TimestampTarget | None = None) -> RetryResult:
    with with_lock(run.staging_dir, run.runtime) as staging:
        return retry_locked(run, staging, expect=expect)
