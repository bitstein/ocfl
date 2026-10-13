"""Staging-area OTS state (`<staging>/ots.json`): the in-memory model."""

import msgspec

from ocfl_cli.model.phase import (  # noqa: TC001 - msgspec decodes these at runtime.
    Phase,
    TimestampTarget,
)

OTS_STATE_VERSION = 2


class Proof(msgspec.Struct, frozen=True, kw_only=True):
    """One requested or received proof, keyed by its digest in `OtsState.proofs`.

    `verified` is False while the proof is requested or pending, True once it
    is final and checked against Bitcoin. A proof outlives the paths that
    asked for it: a staged file with the same digest reuses it (ADR-0004).
    """

    target: TimestampTarget
    ots_path: str | None = None
    verified: bool = False
    block_height: int | None = None
    block_hash: str | None = None
    confirmations_at_verify: int | None = None
    verified_at: str | None = None


class Submission(msgspec.Struct, frozen=True):
    """A round of `/api/proof/timestamp/hashes` POSTs."""

    submitted_at: str
    digests: tuple[str, ...]
    status: str  # "submitting" | "submitted" | "failed"
    target: TimestampTarget
    job_id: str | None = None
    attempts: int = 0
    batch_id: str | None = None
    record_ids: tuple[str | None, ...] | None = None


class CalendarAttestation(msgspec.Struct, frozen=True):
    calendar: str
    block_height: int
    transaction: str
    merkle_root: str


class VerifyJob(msgspec.Struct, frozen=True):
    """An outstanding `verify-batches` job awaiting download/verify."""

    job_id: str
    digests: tuple[str, ...]
    # Canonical SimpleProof statuses; unknown future statuses remain loadable.
    status: str
    total_hashes: int | None = None
    verified_hashes: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
    results_url: str | None = None
    last_snapshot_updated_at: str | None = None
    submitted_hashes: int | None = None
    total_documents_found: int | None = None
    attestation_snapshots: dict[str, tuple[CalendarAttestation, ...]] = msgspec.field(
        default_factory=dict
    )


VERIFY_JOB_STATUSES = frozenset(
    {"PENDING", "IN_PROGRESS", "PROCESSING", "COMPLETED", "FAILED", "EXPIRED"}
)


class StagedInventory(msgspec.Struct, frozen=True):
    """`inventory`: the staged inventory being timestamped (`plan.json`).

    Its proof is `OtsState.proofs[digest]`. Cleared when the Staging Area
    moves back before inventory-pending; the proof stays.
    """

    version: str
    digest: str
    job_id: str | None = None
    batch_id: str | None = None
    record_id: str | None = None
    batch_id: str | None = None
    record_id: str | None = None


class OtsState(msgspec.Struct, frozen=True):
    """In-memory representation of `<staging>/ots.json`.

    Frozen so callers must rebuild via `msgspec.structs.replace` to advance
    phase or append submissions.

    Persisted via `save_ots` (atomic write-temp + fsync + replace).
    """

    phase: Phase
    category_id: int
    schema_version: int = OTS_STATE_VERSION
    pending_digests: tuple[str, ...] = ()
    submissions: tuple[Submission, ...] = ()
    verify_jobs: tuple[VerifyJob, ...] = ()
    proofs: dict[str, Proof] = msgspec.field(default_factory=dict)
    inventory: StagedInventory | None = None

    def proof(self, digest: str) -> Proof | None:
        return self.proofs.get(digest)

    def verified(self, digest: str) -> bool:
        proof = self.proofs.get(digest)
        return proof is not None and proof.verified

    @property
    def inventory_proof(self) -> Proof | None:
        """The proof of the staged inventory, if one is requested or received."""
        return (
            None if self.inventory is None else self.proofs.get(self.inventory.digest)
        )
