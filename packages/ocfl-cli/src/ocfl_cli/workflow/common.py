"""Helpers the staging use cases share: verify jobs, proof bookkeeping, progress."""

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
    VerifiedProof,
    VerifyJob,
)
from ocfl_cli.store.ots_state import (
    save_ots,
)
from ocfl_lib import (
    VersionInfo,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ocfl_cli.model.phase import Phase
    from ocfl_cli.model.simpleproof import (
        CalendarAttestation as WireCalendarAttestation,
    )
    from ocfl_cli.model.simpleproof import VerifyBatchAck
    from ocfl_cli.ports import (
        SimpleProofClient,
    )
    from ocfl_lib import Inventory

logger = logging.getLogger(__name__)


# Inventory head-state shape: digest -> logical paths.
type StateMap = dict[str, list[str]]


LOCK_TIMEOUT_SECONDS = 30


def lock_timeout() -> float:
    """Seconds to wait for a staging or commit lock (read at call time)."""
    return LOCK_TIMEOUT_SECONDS


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


def non_ots_digests(state: StateMap) -> set[str]:
    """Digests with at least one logical path not ending in `.ots`."""
    return {
        d for d, paths in state.items() if any(not p.endswith(".ots") for p in paths)
    }


def is_failed_verify_job(status: str) -> bool:
    # EXPIRED is dead like FAILED: the job will never yield a result, so its
    # digests are not covered and must be resubmitted (cf. batch.py terminal
    # set). Treating it as still-active stalls per-object --upgrade forever.
    return status in {"FAILED", "EXPIRED"}


def is_terminal_verify_job(status: str) -> bool:
    return status in {"COMPLETED", "FAILED", "EXPIRED"}


def verify_job_from_ack(
    ack: VerifyBatchAck,
    digests: tuple[str, ...],
) -> VerifyJob:
    return VerifyJob(
        job_id=ack.job_id,
        digests=digests,
        status="IN_PROGRESS",
        submitted_hashes=ack.submitted_hashes,
        total_documents_found=ack.total_documents_found,
    )


def unverified_digests(state: OtsState, digests: tuple[str, ...]) -> list[str]:
    """Subset of *digests* that do not yet have a verified=True proof."""
    return [
        d for d in digests if not state.content_proofs.get(d, VerifiedProof()).verified
    ]


def require_verified_content_before_inventory(state: OtsState) -> None:
    unverified = unverified_digests(state, state.pending_digests)
    if unverified:
        raise PhaseError(
            f"{len(unverified)} content proof(s) unverified after reorg; "
            "re-run 'stage timestamp content --upgrade' before "
            "timestamping inventory or committing"
        )


def update_head_state(inv: Inventory, state: StateMap) -> Inventory:
    """Return new Inventory with updated head version state."""
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
    """Refresh `status` for every non-terminal verify_job."""
    jobs: list[VerifyJob] = []
    for job in state.verify_jobs:
        if is_terminal_verify_job(job.status):
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


def _parse_iso_seconds(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        created = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return max(0, int((datetime.now(UTC) - created).total_seconds()))


def compute_job_progress(state: OtsState) -> tuple[JobProgress, ...]:
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
                age_seconds=_parse_iso_seconds(job.created_at),
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


def compute_inventory_job_progress(state: OtsState) -> JobProgress | None:
    inv = state.inventory_proof
    if inv is None:
        return None
    progress = {job.job_id: job for job in compute_job_progress(state)}
    for job in state.verify_jobs:
        if inv.digest in job.digests:
            return progress.get(job.job_id)
    return None


def record_proof(state: OtsState, digest: str, proof: VerifiedProof) -> OtsState:
    proofs = dict(state.content_proofs)
    proofs[digest] = proof
    return replace(state, content_proofs=proofs)
