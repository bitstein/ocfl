"""Staging-area OTS state (`<staging>/ots.json`): the in-memory model."""

import msgspec

from ocfl_cli.model.phase import Phase  # noqa: TC001 - msgspec decodes this at runtime.

OTS_STATE_VERSION = 1


class VerifiedProof(msgspec.Struct, frozen=True):
    """Shared sub-shape: present in every `content_proofs` entry and in
    `inventory_proof`. Optional additions must preserve both wire shapes;
    breaking changes require a schema bump.
    """

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


class InventoryProof(msgspec.Struct, frozen=True):
    """`inventory_proof`: inventory digest + version + verify job_id plus a
    nested shared `VerifiedProof`. The JSON wire shape flattens `proof`
    into the top level of `inventory_proof` (see plan §`ots.json` schema)."""

    version: str
    digest: str
    proof: VerifiedProof = msgspec.field(default_factory=VerifiedProof)
    job_id: str | None = None
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
    content_proofs: dict[str, VerifiedProof] = msgspec.field(default_factory=dict)
    inventory_proof: InventoryProof | None = None
