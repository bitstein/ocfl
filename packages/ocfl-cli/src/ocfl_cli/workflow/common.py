import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from msgspec.structs import replace

from ocfl_cli.errors import (
    AttestationMismatchError,
    PhaseError,
)
from ocfl_cli.model.ots_state import (
    CalendarAttestation,
    OtsState,
    Proof,
    VerifyJob,
)
from ocfl_cli.model.simpleproof import TERMINAL_VERIFY_STATUSES, VerifyJobStatus
from ocfl_cli.store.ingest import ingest_file
from ocfl_cli.store.ots_state import (
    ots_cache_path,
    save_ots,
)
from ocfl_lib import (
    VersionInfo,
    ots,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ocfl_cli.model.content import ContentMap
    from ocfl_cli.model.phase import Phase, TimestampTarget
    from ocfl_cli.model.simpleproof import (
        CalendarAttestation as WireCalendarAttestation,
    )
    from ocfl_cli.model.simpleproof import VerifyBatchAck
    from ocfl_cli.ots.bitcoin import VerifiedAttestation
    from ocfl_cli.ports import (
        Runtime,
        SimpleProofClient,
    )
    from ocfl_lib import DigestAlgorithm, Inventory, StorageRoot

logger = logging.getLogger(__name__)


# Inventory head-state shape: digest -> logical paths.
type StateMap = dict[str, list[str]]


@dataclass(frozen=True, slots=True)
class JobProgress:
    job_id: str
    status: str
    total_hashes: int | None
    verified_hashes: int | None
    age_seconds: int | None
    anchored_digests: int
    pending_digests: int
    calendar_counts: tuple[tuple[str, int], ...]
    submitted_hashes: int | None = None
    total_documents_found: int | None = None


def log_phase(prev: Phase, new: Phase) -> None:
    if new is not prev:
        logger.info("phase: %s -> %s", prev.value, new.value)


def ordinary_paths(state: StateMap) -> dict[str, str]:
    digest_of = {p: d for d, paths in state.items() for p in paths}
    ordinary, _proofs = ots.split_state(digest_of)
    return {p: digest_of[p] for p in sorted(ordinary)}


def ordinary_digests(state: StateMap) -> set[str]:
    return set(ordinary_paths(state).values())


type CarriedProofs = dict[str, str]


def carried_proofs(sr: StorageRoot, object_id: str) -> CarriedProofs:
    obj = sr.get_object(object_id)
    if not obj.exists:
        return {}
    cfg = ots.read_config(obj.fs, obj.path)
    if cfg is None or not cfg.timestamp_content:
        return {}
    inv = obj.inventory.inventory
    carried: CarriedProofs = {}
    for v in ots.covered_versions(cfg, inv.versions):
        digest_of = {p: d for d, ps in inv.versions[v].state.items() for p in ps}
        _ordinary, proofs = ots.split_state(digest_of)
        for proof_path, companion in proofs.items():
            carried.setdefault(digest_of[companion], digest_of[proof_path])
    return carried


def required_content(state: StateMap) -> dict[str, list[str]]:
    need: dict[str, list[str]] = {}
    for p, d in ordinary_paths(state).items():
        need.setdefault(d, []).append(p)
    return need


def proof_cache_ready(state: OtsState, staging_dir: Path, digest: str) -> bool:
    return state.verified(digest) and ots_cache_path(staging_dir, digest).is_file()


def proof_ready(
    state: OtsState, staging_dir: Path, carried: CarriedProofs, digest: str
) -> bool:
    return digest in carried or proof_cache_ready(state, staging_dir, digest)


def place_companions(
    state: StateMap,
    need: dict[str, list[str]],
    ots_state: OtsState,
    carried: CarriedProofs,
    *,
    staging_dir: Path,
    content_map: ContentMap,
    alg: DigestAlgorithm,
) -> bool:
    """Give every path of a proved digest its companion `P.ots`; True if any changed.

    The companion is the object's committed proof of the digest, or else the
    cached verified one. A path keeps the proof of its digest wherever it
    moves, so a renamed or re-added file needs no new submission. Idempotent:
    it runs on every pass, so a crash after the proof was recorded and before
    its companions were placed heals on the next one.
    """
    changed = False
    for d, paths in need.items():
        if d in carried:
            ots_digest = carried[d]
        elif proof_cache_ready(ots_state, staging_dir, d):
            ots_digest = ingest_file(
                ots_cache_path(staging_dir, d),
                f"{paths[0]}{ots.PROOF_SUFFIX}",
                staging_dir=staging_dir,
                content_map=content_map,
                digest_alg=alg,
            ).digest
        else:
            continue
        for p in paths:
            companion = f"{p}{ots.PROOF_SUFFIX}"
            if companion in state.get(ots_digest, ()):
                continue
            for other in list(state):
                if companion in state[other]:
                    state[other].remove(companion)
                    if not state[other]:
                        del state[other]
            state.setdefault(ots_digest, []).append(companion)
            changed = True
    return changed


def verify_job_from_ack(
    ack: VerifyBatchAck,
    digests: tuple[str, ...],
) -> VerifyJob:
    return VerifyJob(
        job_id=ack.job_id,
        digests=digests,
        status=VerifyJobStatus.IN_PROGRESS,
        submitted_hashes=ack.submitted_hashes,
        total_documents_found=ack.total_documents_found,
    )


def unverified_digests(state: OtsState, digests: tuple[str, ...]) -> list[str]:
    return [d for d in digests if not state.verified(d)]


def require_verified_content_before_inventory(state: OtsState) -> None:
    unverified = unverified_digests(state, state.pending_digests)
    if unverified:
        raise PhaseError(
            f"{len(unverified)} content proof(s) unverified after reorg; "
            "upgrade the content proofs again before "
            "timestamping inventory or committing"
        )


def update_head_state(inv: Inventory, state: StateMap) -> Inventory:
    from ocfl_lib import Inventory

    head = inv.head
    vi = inv.versions[head]
    updated_versions = dict(inv.versions)
    updated_versions[head] = VersionInfo(
        created=vi.created, state=state, message=vi.message, user=vi.user
    )
    return Inventory(
        id=inv.id,
        type=inv.type,
        digest_algorithm=inv.digest_algorithm,
        head=inv.head,
        manifest=inv.manifest,
        versions=updated_versions,
        content_directory=inv.content_directory,
        fixity=inv.fixity,
    )


def poll_outstanding_jobs(state: OtsState, sp: SimpleProofClient) -> OtsState:
    jobs: list[VerifyJob] = []
    for job in state.verify_jobs:
        if job.status in TERMINAL_VERIFY_STATUSES:
            jobs.append(job)
            continue
        poll = sp.poll_job(job.job_id)
        jobs.append(
            replace(
                job,
                status=poll.status,
                total_hashes=poll.total_hashes,
                verified_hashes=poll.verified_hashes,
                created_at=poll.created_at,
                updated_at=poll.updated_at,
                results_url=poll.results_url,
            )
        )
    polled = tuple(jobs)
    return replace(state, verify_jobs=polled)


def _state_attestations(
    attestations: tuple[WireCalendarAttestation, ...],
) -> tuple[CalendarAttestation, ...]:
    return tuple(
        CalendarAttestation(
            calendar=a.calendar,
            block_height=a.block_height,
            transaction=a.transaction,
            merkle_root=a.merkle_root,
        )
        for a in attestations
    )


def _should_fetch_snapshot(job: VerifyJob) -> bool:
    return job.results_url is not None and (
        job.last_snapshot_updated_at is None
        or job.updated_at is None
        or job.updated_at != job.last_snapshot_updated_at
    )


def snapshot_results(
    state: OtsState, staging_dir: Path, sp: SimpleProofClient
) -> OtsState:
    jobs = list(state.verify_jobs)
    changed = False
    for i, job in enumerate(jobs):
        if not _should_fetch_snapshot(job) or job.results_url is None:
            continue
        submitted = set(job.digests)
        snapshots = dict(job.attestation_snapshots)
        for entry in sp.fetch_results_entries(job.results_url):
            if entry.digest not in submitted:
                raise AttestationMismatchError(
                    f"unexpected digest from SP: {entry.digest} "
                    f"(not in job {job.job_id} submission set)"
                )
            if not entry.confirmed or not entry.attestations:
                continue
            snapshots[entry.digest] = _state_attestations(entry.attestations)
        jobs[i] = replace(
            job,
            attestation_snapshots=snapshots,
            last_snapshot_updated_at=job.updated_at,
        )
        changed = True
    if not changed:
        return state
    state = replace(state, verify_jobs=tuple(jobs))
    save_ots(staging_dir, state)
    return state


def _parse_iso_seconds(value: str | None, *, runtime: Runtime) -> int | None:
    if value is None:
        return None
    try:
        created = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return max(0, int((runtime.clock.now() - created).total_seconds()))


def compute_job_progress(
    state: OtsState, *, runtime: Runtime
) -> tuple[JobProgress, ...]:
    progress: list[JobProgress] = []
    for job in state.verify_jobs:
        calendar_counts: dict[str, int] = {}
        for attestations in job.attestation_snapshots.values():
            for attestation in attestations:
                calendar_counts[attestation.calendar] = (
                    calendar_counts.get(attestation.calendar, 0) + 1
                )
        progress.append(
            JobProgress(
                job_id=job.job_id,
                status=job.status,
                total_hashes=job.total_hashes,
                verified_hashes=job.verified_hashes,
                age_seconds=_parse_iso_seconds(job.created_at, runtime=runtime),
                anchored_digests=len(job.attestation_snapshots),
                pending_digests=max(
                    0, len(job.digests) - len(job.attestation_snapshots)
                ),
                calendar_counts=tuple(
                    sorted(
                        calendar_counts.items(), key=lambda item: (-item[1], item[0])
                    )
                ),
                submitted_hashes=job.submitted_hashes,
                total_documents_found=job.total_documents_found,
            )
        )
    return tuple(progress)


def compute_inventory_job_progress(
    state: OtsState, *, runtime: Runtime
) -> JobProgress | None:
    inv = state.inventory
    if inv is None:
        return None
    progress = {job.job_id: job for job in compute_job_progress(state, runtime=runtime)}
    for job in state.verify_jobs:
        if inv.digest in job.digests:
            return progress.get(job.job_id)
    return None


def record_proof(state: OtsState, digest: str, proof: Proof) -> OtsState:
    proofs = dict(state.proofs)
    proofs[digest] = proof
    return replace(state, proofs=proofs)


def request_proofs(
    state: OtsState, digests: tuple[str, ...], target: TimestampTarget
) -> OtsState:
    missing = [d for d in digests if d not in state.proofs]
    if not missing:
        return state
    proofs = dict(state.proofs)
    for d in missing:
        proofs[d] = Proof(target=target)
    return replace(state, proofs=proofs)


def unverify(proof: Proof) -> Proof:
    return Proof(target=proof.target, ots_path=proof.ots_path)


def verified_proof(
    target: TimestampTarget,
    ots_path: str | None,
    result: VerifiedAttestation,
    verified_at: str | None,
) -> Proof:
    return Proof(
        target=target,
        ots_path=ots_path,
        verified=True,
        block_height=result.block_height,
        block_hash=result.block_hash,
        confirmations_at_verify=result.confirmations,
        verified_at=verified_at,
    )
