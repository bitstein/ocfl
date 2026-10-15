from typing import Literal

import msgspec

from ocfl_cli.model.phase import (  # noqa: TC001 - msgspec decodes these at runtime.
    Phase,
    TimestampTarget,
)

OTS_STATE_VERSION = 2

SubmissionStatus = Literal["submitting", "submitted", "failed"]


class Proof(msgspec.Struct, frozen=True, kw_only=True):
    target: TimestampTarget
    ots_path: str | None = None
    verified: bool = False
    block_height: int | None = None
    block_hash: str | None = None
    confirmations_at_verify: int | None = None
    verified_at: str | None = None


class Submission(msgspec.Struct, frozen=True):
    submitted_at: str
    digests: tuple[str, ...]
    status: SubmissionStatus
    target: TimestampTarget
    attempts: int = 0
    batch_id: str | None = None
    record_ids: tuple[str | None, ...] | None = None


class CalendarAttestation(msgspec.Struct, frozen=True):
    calendar: str
    block_height: int
    transaction: str
    merkle_root: str


class VerifyJob(msgspec.Struct, frozen=True):
    job_id: str
    digests: tuple[str, ...]
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


class StagedInventory(msgspec.Struct, frozen=True):
    version: str
    digest: str
    job_id: str | None = None


class OtsState(msgspec.Struct, frozen=True):
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
        return (
            None if self.inventory is None else self.proofs.get(self.inventory.digest)
        )
