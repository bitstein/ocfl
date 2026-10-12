"""Workflow orchestration for the stage command group.

Pure (typer-free) functions that coordinate the OTS workflow against
SimpleProof + Bitcoin RPC + the staging store. Typer callbacks in
`stage/commands.py` translate user options into calls here and render
returned data; watch loops in `stage/watch.py` reuse these primitives
under a ZMQ-driven block loop.
"""

import dataclasses
import hashlib
import logging
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

import filelock
from msgspec.structs import replace

from ocfl_cli.errors import (
    AttestationMismatchError,
    BitcoinError,
    CommitFailedError,
    InventoryProofMissingError,
    LockTimeoutError,
    MissingStagedMetadataError,
    PhaseError,
    StagingFileChangedError,
    StagingFileMissingError,
)
from ocfl_cli.ots.bitcoin import (
    BitcoinRPC,
    BlockchainInfo,
    PendingVerification,
    VerifiedAttestation,
    verify_attestation,
)
from ocfl_cli.ots.simpleproof import _SUBMIT_CHUNK_SIZE
from ocfl_cli.stage import store as _store_mod
from ocfl_cli.stage.fsm import Phase, check_transition
from ocfl_cli.stage.ingest import ingest_file
from ocfl_cli.stage.schemas import NextActionKind, StagedMetadata, StagedUser
from ocfl_cli.stage.state import (
    CalendarAttestation,
    InventoryProof,
    OtsState,
    Submission,
    VerifiedProof,
    VerifyJob,
    apply_category_id,
    load_ots,
    now_utc_iso,
    ots_cache_path,
    plan_path,
    save_ots,
    with_lock,
)
from ocfl_lib import (
    CommitPlan,
    OtsParseError,
    PlanMismatchError,
    UserInfo,
    VersionInfo,
)
from ocfl_lib.ots import parse as parse_ots

if TYPE_CHECKING:
    from collections.abc import Callable

    from ocfl_cli._appctx import BtcRpcFactory, SpClientFactory
    from ocfl_cli.config import Config
    from ocfl_cli.ots.simpleproof import (
        CalendarAttestation as WireCalendarAttestation,
    )
    from ocfl_cli.ots.simpleproof import (
        SimpleProofClient,
        VerifyBatchAck,
    )
    from ocfl_cli.stage.store import StagingStore
    from ocfl_lib import ContentSource, DigestAlgorithm, Inventory, StorageRoot
    from ocfl_lib.fs import FS, WriteFS


logger = logging.getLogger(__name__)

# Inventory head-state shape: digest -> logical paths.
type StateMap = dict[str, list[str]]

_LOCK_TIMEOUT_SECONDS = 30
_OBJECT_LOCK_DIR = "ocfl-cli-object-locks"

_NEXT_ACTION_HUMAN: dict[NextActionKind, str] = {
    NextActionKind.ADD_CONTENT: "stage add <path>",
    NextActionKind.TIMESTAMP_CONTENT: "stage timestamp content",
    NextActionKind.RETRY_CONTENT: "stage timestamp content --retry",
    NextActionKind.UPGRADE_CONTENT: (
        "stage timestamp content --upgrade (or --upgrade --watch)"
    ),
    NextActionKind.TIMESTAMP_INVENTORY: "stage timestamp inventory",
    NextActionKind.RETRY_INVENTORY: "stage timestamp inventory --retry",
    NextActionKind.UPGRADE_INVENTORY: (
        "stage timestamp inventory --upgrade (or --upgrade --watch)"
    ),
    NextActionKind.COMMIT: "stage commit",
    NextActionKind.NONE: "",
}


class _OutstandingJobJson(TypedDict):
    job_id: str
    digests: list[str]
    status: str
    submitted_at: str
    age_seconds: int


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


@dataclass(frozen=True, slots=True)
class UpgradeContentResult:
    previous_phase: Phase
    phase: Phase
    unverified_pending_count: int = 0
    orphan_count: int = 0
    jobs: tuple[JobProgress, ...] = ()


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


class MetadataInputError(ValueError):
    """Invalid staged metadata CLI input."""


# ---------------------------------------------------------------------------
# Small workflow utilities.
# ---------------------------------------------------------------------------


def _log_phase(prev: Phase, new: Phase) -> None:
    if new is not prev:
        logger.info("phase: %s -> %s", prev.value, new.value)


def _non_ots_digests(state: StateMap) -> set[str]:
    """Digests with at least one logical path not ending in `.ots`."""
    return {
        d for d, paths in state.items() if any(not p.endswith(".ots") for p in paths)
    }


def _covered_digests(state: OtsState) -> set[str]:
    """Digests already covered by a non-FAILED verify_job."""
    return {
        d
        for j in state.verify_jobs
        if not _is_failed_verify_job(j.status)
        for d in j.digests
    }


def _is_failed_verify_job(status: str) -> bool:
    # EXPIRED is dead like FAILED: the job will never yield a result, so its
    # digests are not covered and must be resubmitted (cf. batch.py terminal
    # set). Treating it as still-active stalls per-object --upgrade forever.
    return status in {"FAILED", "EXPIRED"}


def _is_terminal_verify_job(status: str) -> bool:
    return status in {"COMPLETED", "FAILED", "EXPIRED"}


def _verify_job_from_ack(
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


def _unverified(state: OtsState, digests: tuple[str, ...]) -> list[str]:
    """Subset of *digests* that do not yet have a verified=True proof."""
    return [
        d for d in digests if not state.content_proofs.get(d, VerifiedProof()).verified
    ]


def _require_verified_content_before_inventory(state: OtsState) -> None:
    unverified = _unverified(state, state.pending_digests)
    if unverified:
        raise PhaseError(
            f"{len(unverified)} content proof(s) unverified after reorg; "
            "re-run 'stage timestamp content --upgrade' before "
            "timestamping inventory or committing"
        )


def _validate_metadata_input(value: str | None, *, field: str) -> str | None:
    if value is not None and not value.strip():
        raise MetadataInputError(f"staged metadata {field} cannot be empty")
    return value


def _check_metadata_conflict(
    *,
    field: str,
    value: str | None,
    clear: bool,
) -> None:
    if value is not None and clear:
        raise MetadataInputError(f"--{field} is incompatible with --clear-{field}")


def _compact_user(name: str | None, address: str | None) -> StagedUser | None:
    if name is None and address is None:
        return None
    return StagedUser(name=name, address=address)


def staged_metadata_from_options(
    *,
    message: str | None,
    name: str | None,
    address: str | None,
) -> StagedMetadata:
    """Build staged metadata from initial CLI options."""
    message = _validate_metadata_input(message, field="message")
    name = _validate_metadata_input(name, field="user.name")
    address = _validate_metadata_input(address, field="user.address")
    return StagedMetadata(
        message=message,
        user=_compact_user(name, address),
    )


def validate_metadata_update_options(
    *,
    message: str | None,
    name: str | None,
    address: str | None,
    clear_message: bool,
    clear_name: bool,
    clear_address: bool,
) -> None:
    """Validate staged metadata update options without reading staging state."""
    _check_metadata_conflict(field="message", value=message, clear=clear_message)
    _check_metadata_conflict(field="name", value=name, clear=clear_name)
    _check_metadata_conflict(field="address", value=address, clear=clear_address)

    _validate_metadata_input(message, field="message")
    _validate_metadata_input(name, field="user.name")
    _validate_metadata_input(address, field="user.address")


def update_metadata(
    store: StagingStore,
    root_path: str,
    object_id: str,
    *,
    message: str | None,
    name: str | None,
    address: str | None,
    clear_message: bool,
    clear_name: bool,
    clear_address: bool,
    before_save: Callable[[StagedMetadata, StagedMetadata], None] | None = None,
) -> StagedMetadata:
    """Apply set/update/clear semantics to staged metadata and persist it.

    ``before_save`` lets callers invalidate dependent state after the new
    metadata is known but before metadata.json is rewritten.
    """
    validate_metadata_update_options(
        message=message,
        name=name,
        address=address,
        clear_message=clear_message,
        clear_name=clear_name,
        clear_address=clear_address,
    )

    current = store.load_metadata(root_path, object_id)
    current_name = current.user.name if current.user is not None else None
    current_address = current.user.address if current.user is not None else None

    next_message = current.message
    if clear_message:
        next_message = None
    if message is not None:
        next_message = message

    next_name = current_name
    if clear_name:
        next_name = None
    if name is not None:
        next_name = name

    next_address = current_address
    if clear_address or (clear_name and address is None):
        next_address = None
    if address is not None:
        next_address = address

    updated = replace(
        current,
        message=next_message,
        user=_compact_user(next_name, next_address),
    )
    if before_save is not None:
        before_save(current, updated)
    store.save_metadata(root_path, object_id, updated)
    return updated


def compute_in_sync(info: BlockchainInfo) -> bool:
    """Bitcoin-in-sync derivation pinned by `OTS workflow contract`."""
    return (not info.initial_block_download) and info.verification_progress >= 0.9999


def humanize_age(seconds: int) -> str:
    """Floor-rounded ASCII single-unit age (`30s ago`, `5m ago`, `2h ago`, `3d ago`)."""
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _status_now() -> datetime:
    """Indirection seam so tests can pin `now` for age math."""
    return datetime.now(UTC)


def _submission_age_seconds(submitted_at: str, now: datetime) -> int:
    if not submitted_at:
        return 0
    try:
        dt = datetime.fromisoformat(submitted_at)
    except ValueError:
        return 0
    return max(0, int((now - dt).total_seconds()))


@contextmanager
def _object_commit_lock(
    root_path: str,
    object_id: str,
    *,
    timeout: float = _LOCK_TIMEOUT_SECONDS,
):
    """Serialize local CLI commits for one storage-root/object pair.

    This is an operator-ergonomics lock: it prevents ordinary same-machine CLI
    commits from racing into retryable stale-head failures. The library still
    enforces optimistic stale-head checks because S3 and non-CLI callers need a
    backend-independent correctness guard.
    """
    key = hashlib.sha256(f"{root_path}\0{object_id}".encode()).hexdigest()
    lock_dir = Path(tempfile.gettempdir()) / _OBJECT_LOCK_DIR
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = filelock.FileLock(str(lock_dir / f"{key}.lock"))
    try:
        lock.acquire(timeout=timeout)
    except filelock.Timeout as e:
        raise LockTimeoutError(
            f"could not acquire commit lock for {object_id!r} within {timeout}s"
        ) from e
    try:
        yield
    finally:
        lock.release()


def next_action_for(state: OtsState, has_files: bool) -> tuple[NextActionKind, str]:
    """FSM-aware `(kind, human)` pair for the OTS status next-action surface."""
    match state.phase:
        case Phase.DRAFTING:
            kind = (
                NextActionKind.TIMESTAMP_CONTENT
                if has_files
                else NextActionKind.ADD_CONTENT
            )
        case Phase.CONTENT_PENDING:
            verified = {d for d, p in state.content_proofs.items() if p.verified}
            still_pending = set(state.pending_digests) - verified
            failed_covers = any(
                _is_failed_verify_job(j.status) and (set(j.digests) & still_pending)
                for j in state.verify_jobs
            )
            kind = (
                NextActionKind.RETRY_CONTENT
                if failed_covers
                else NextActionKind.UPGRADE_CONTENT
            )
        case Phase.CONTENT_UPGRADED:
            kind = NextActionKind.TIMESTAMP_INVENTORY
        case Phase.INVENTORY_PENDING:
            ip = state.inventory_proof
            kind = (
                NextActionKind.RETRY_INVENTORY
                if ip is not None
                and any(
                    _is_failed_verify_job(j.status) and ip.digest in j.digests
                    for j in state.verify_jobs
                )
                else NextActionKind.UPGRADE_INVENTORY
            )
        case Phase.INVENTORY_UPGRADED:
            kind = NextActionKind.COMMIT
        case Phase.COMMITTED:
            kind = NextActionKind.NONE
        case _:
            raise RuntimeError(f"unhandled phase: {state.phase!r}")
    return kind, _NEXT_ACTION_HUMAN[kind]


# ---------------------------------------------------------------------------
# Inventory helpers.
# ---------------------------------------------------------------------------


def _update_head_state(inv: Inventory, state: StateMap) -> Inventory:
    """Return new Inventory with updated head version state."""
    from ocfl_lib import Inventory, VersionInfo

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


class _StagingContentSource:
    """ContentSource resolving digests against the staging content map."""

    def __init__(self, cm: dict[str, dict]) -> None:
        self._cm = cm

    def get_content(self, digest: str) -> tuple[FS, str]:
        from ocfl_lib import LocalFS

        if digest in self._cm:
            file_path = Path(self._cm[digest]["path"])
            return LocalFS(file_path.parent), file_path.name
        raise KeyError(digest)


def _validate_and_build_content_source(
    store: StagingStore,
    root_path: str,
    object_id: str,
) -> _StagingContentSource:
    """Re-load content_map, validate stale-file size+mtime, build source."""
    content_map = store.load_content_map(root_path, object_id)
    for entry in content_map.values():
        file_path = Path(entry["path"])
        if not file_path.exists():
            raise StagingFileMissingError(f"source file missing: {file_path}")
        stat = file_path.stat()
        if stat.st_size != entry["size"] or stat.st_mtime != entry["mtime"]:
            raise StagingFileChangedError(
                f"source file changed since staging: {file_path}"
            )
    return _StagingContentSource(content_map)


def build_inventory_plan(
    store: StagingStore,
    sr,
    root_path: str,
    object_id: str,
    *,
    message: str | None = None,
    user: UserInfo | None = None,
    created: str | None = None,
) -> tuple[CommitPlan, ContentSource]:
    """Build a CommitPlan from the staged inventory + content map.

    Used by both `stage commit` (which commits it) and `stage timestamp
    inventory` (which saves it as `plan.json` for a later commit). Returns
    the plan and the `_StagingContentSource` the commit reads content from.
    """
    from ocfl_lib import DigestAlgorithm, VersionState

    inv = store.load_inventory(root_path, object_id)
    alg = DigestAlgorithm.from_ocfl_name(inv.digest_algorithm)
    head = inv.head

    content_source = _validate_and_build_content_source(store, root_path, object_id)
    stage_obj = VersionState(
        state=inv.versions[head].state,
        digest_algorithm=alg,
        content_source=content_source,
    )
    plan = sr.plan(object_id, stage_obj, message=message, user=user, created=created)
    return plan, content_source


def load_plan(staging_dir: Path) -> CommitPlan | None:
    """The staged `plan.json`, or None if it is missing or unreadable."""
    try:
        return CommitPlan.from_json(plan_path(staging_dir).read_bytes())
    except FileNotFoundError, PlanMismatchError:
        return None


# ---------------------------------------------------------------------------
# rm / mv companion + regression helpers.
# ---------------------------------------------------------------------------


def _pair_ots_companion_remove(state: StateMap, removed_paths: list[str]) -> None:
    """Remove `<path>.ots` companions from *state* for each non-`.ots` path removed."""
    for p in removed_paths:
        if p.endswith(".ots"):
            continue
        companion = f"{p}.ots"
        for d in list(state):
            if companion in state[d]:
                state[d].remove(companion)
                if not state[d]:
                    del state[d]
                break


def _pair_ots_companion_rename(state: StateMap, src: str, dst: str) -> None:
    """Rename `<src>.ots` to `<dst>.ots` within *state* if present."""
    if src.endswith(".ots"):
        return
    src_ots = f"{src}.ots"
    dst_ots = f"{dst}.ots"
    for d in state:
        if src_ots in state[d]:
            state[d].remove(src_ots)
            state[d].append(dst_ots)
            return


def _apply_rm_mv_regression(
    staging_dir: Path,
    new_inv_state: StateMap,
    command: str,
    cause: str,
) -> None:
    """Update `ots.json` per regression rules for stage rm/mv. Caller holds the lock."""
    state = load_ots(staging_dir)
    new_phase = check_transition(state.phase, command)
    new_pending = tuple(
        d for d in state.pending_digests if d in _non_ots_digests(new_inv_state)
    )
    new_inv_proof = state.inventory_proof
    if state.phase in (Phase.INVENTORY_PENDING, Phase.INVENTORY_UPGRADED):
        new_inv_proof = None
        plan_path(staging_dir).unlink(missing_ok=True)
    save_ots(
        staging_dir,
        replace(
            state,
            phase=new_phase,
            pending_digests=new_pending,
            inventory_proof=new_inv_proof,
        ),
    )
    if new_phase is not state.phase:
        logger.info(
            "phase regression %s→%s (cause: %s)",
            state.phase.value,
            new_phase.value,
            cause,
        )


def apply_metadata_ots_regression(staging_dir: Path) -> None:
    """Invalidate stale inventory timestamp state after staged metadata changes.

    Metadata affects the inventory bytes but not staged content bytes. Once an
    inventory timestamp exists, regress only the inventory side of the workflow
    so verified content proofs remain reusable.
    """
    state = load_ots(staging_dir)
    if state.phase not in (Phase.INVENTORY_PENDING, Phase.INVENTORY_UPGRADED):
        return

    inventory_digest = (
        state.inventory_proof.digest if state.inventory_proof is not None else None
    )
    inventory_job_id = (
        state.inventory_proof.job_id if state.inventory_proof is not None else None
    )
    kept_jobs = tuple(
        job
        for job in state.verify_jobs
        if (inventory_digest is None or inventory_digest not in job.digests)
        and (inventory_job_id is None or job.job_id != inventory_job_id)
    )
    save_ots(
        staging_dir,
        replace(
            state,
            phase=Phase.CONTENT_UPGRADED,
            verify_jobs=kept_jobs,
            inventory_proof=None,
        ),
    )
    plan_path(staging_dir).unlink(missing_ok=True)
    logger.info(
        "phase regression %s→%s (cause: staged metadata edit)",
        state.phase.value,
        Phase.CONTENT_UPGRADED.value,
    )


# ---------------------------------------------------------------------------
# Status data builders (callers render).
# ---------------------------------------------------------------------------


def _cache_bytes(staging_dir: Path) -> int:
    cache_dir = staging_dir / "ots-cache"
    if not cache_dir.is_dir():
        return 0
    return sum(
        entry.stat().st_size
        for entry in cache_dir.iterdir()
        if entry.is_file() and not entry.is_symlink()
    )


def _submission_submitted_at(state: OtsState, digests: tuple[str, ...]) -> str:
    """Earliest submitted_at among submissions overlapping *digests*.

    Production never back-fills Submission.job_id (see make_content_submission),
    so a verify job is matched to its originating submission by digest overlap
    rather than job_id; joining on job_id always missed, rendering every
    outstanding job '0s ago'. ISO-8601 UTC strings sort chronologically.
    """
    target = set(digests)
    times = [
        s.submitted_at
        for s in state.submissions
        if s.submitted_at and target & set(s.digests)
    ]
    return min(times) if times else ""


def _build_outstanding_jobs(
    state: OtsState, now: datetime
) -> list[_OutstandingJobJson]:
    outstanding: list[_OutstandingJobJson] = []
    for j in state.verify_jobs:
        if _is_terminal_verify_job(j.status):
            continue
        submitted_at = _submission_submitted_at(state, j.digests)
        outstanding.append(
            {
                "job_id": j.job_id,
                "digests": list(j.digests),
                "status": j.status,
                "submitted_at": submitted_at,
                "age_seconds": _submission_age_seconds(submitted_at, now),
            }
        )
    return outstanding


def _build_counts(state: OtsState, non_ots: set[str]) -> dict[str, int]:
    total = len(non_ots)
    upgraded_set = {d for d, p in state.content_proofs.items() if p.verified}
    upgraded = len(upgraded_set & non_ots) if non_ots else 0

    non_failed_set: set[str] = set()
    failed_set: set[str] = set()
    for j in state.verify_jobs:
        (failed_set if _is_failed_verify_job(j.status) else non_failed_set).update(
            j.digests
        )
    return {
        "total": total,
        "submitted": len(non_failed_set),
        "upgraded": upgraded,
        "pending": total - upgraded,
        "failed": len(failed_set - non_failed_set),
    }


def _build_bitcoin_section(
    cfg: Config | None, btc_factory: BtcRpcFactory
) -> dict[str, object] | None:
    if cfg is None or cfg.bitcoin is None:
        return None
    try:
        info = btc_factory(cfg).getblockchaininfo()
    except BitcoinError as e:
        logger.warning("bitcoin RPC unreachable, omitting bitcoin section: %s", e)
        return None
    return {
        "height": info.height,
        "tip_hash": info.best_block_hash,
        "in_sync": compute_in_sync(info),
        "confirmations_required": cfg.bitcoin.confirmations,
    }


def metadata_status_dict(
    store: StagingStore, root_path: str, object_id: str
) -> dict[str, str | None]:
    """Return saved values for required staged metadata fields."""
    metadata = store.load_metadata(root_path, object_id)
    user = metadata.user
    return {
        "message": metadata.message,
        "user.name": user.name if user is not None else None,
        "user.address": user.address if user is not None else None,
    }


def missing_metadata_fields(metadata: StagedMetadata) -> list[str]:
    """Return required staged metadata fields that are not present."""
    user = metadata.user
    missing: list[str] = []
    if metadata.message is None:
        missing.append("message")
    if user is None or user.name is None:
        missing.append("user.name")
    if user is None or user.address is None:
        missing.append("user.address")
    return missing


def require_staged_metadata(
    metadata: StagedMetadata, *, allow_missing_metadata: bool
) -> None:
    """Enforce required staged metadata unless the caller explicitly bypasses."""
    if allow_missing_metadata:
        return
    missing = missing_metadata_fields(metadata)
    if missing:
        fields = ", ".join(missing)
        raise MissingStagedMetadataError(
            f"missing staged metadata: {fields} "
            "(use --allow-missing-metadata to bypass)"
        )


def require_timestamped_plan_metadata(
    version: VersionInfo, *, allow_missing_metadata: bool
) -> None:
    """Enforce required metadata on the immutable timestamped inventory plan."""
    if allow_missing_metadata:
        return
    missing = missing_metadata_fields(_staged_metadata_from_version(version))
    if missing:
        fields = ", ".join(missing)
        raise MissingStagedMetadataError(
            f"timestamped inventory plan missing metadata: {fields} "
            "(rerun stage timestamp inventory after fixing staged metadata, "
            "or use --allow-missing-metadata to bypass)"
        )


def _version_user_from_staged(metadata: StagedMetadata) -> UserInfo | None:
    user = metadata.user
    if user is None or user.name is None:
        return None
    return UserInfo(name=user.name, address=user.address)


def _staged_metadata_from_version(version: VersionInfo) -> StagedMetadata:
    user = version.user
    return StagedMetadata(
        message=version.message,
        user=None if user is None else StagedUser(name=user.name, address=user.address),
    )


def status_ots_dict(
    cfg: Config | None,
    store: StagingStore,
    root_path: str,
    object_id: str,
    staging_dir: Path,
    *,
    btc_factory: BtcRpcFactory,
) -> dict[str, Any]:
    """Build the JSON-shaped OTS status payload (commands.py renders human form)."""
    state = load_ots(staging_dir)
    inv = store.load_inventory(root_path, object_id)
    head_state = inv.versions[inv.head].state

    non_ots = _non_ots_digests(head_state)
    counts = _build_counts(state, non_ots)
    has_files = counts["total"] > 0

    kind, human = next_action_for(state, has_files)
    diff = status_diff_dict(store, root_path, object_id)

    return {
        "schema_version": 1,
        "extension": "NNNN-ots-timestamps",
        "phase": state.phase.value,
        "changes": diff["changes"],
        "metadata": diff["metadata"],
        "counts": counts,
        "outstanding_jobs": _build_outstanding_jobs(state, _status_now()),
        "bitcoin": _build_bitcoin_section(cfg, btc_factory),
        "simpleproof": {
            "configured": cfg is not None and cfg.simpleproof is not None,
            "category_id": state.category_id,
        },
        "cache_bytes": _cache_bytes(staging_dir),
        "next_action_kind": kind.value,
        "next_action": human,
    }


def status_diff_dict(
    store: StagingStore, root_path: str, object_id: str
) -> dict[str, Any]:
    """Build the staged-vs-prior diff payload."""
    from ocfl_lib import VNum, to_path_map

    inv = store.load_inventory(root_path, object_id)
    head = inv.head

    staged_pm = to_path_map(inv.versions[head].state)
    head_vnum = VNum.parse(head)
    if head_vnum.number > 1:
        prev_v = str(VNum(head_vnum.number - 1))
        prior_pm = to_path_map(inv.versions[prev_v].state)
    else:
        prior_pm: dict[str, str] = {}

    all_paths = set(staged_pm) | set(prior_pm)
    changes: list[dict[str, str]] = []
    added: dict[str, str] = {}
    removed: dict[str, str] = {}

    for p in sorted(all_paths):
        in_prior = p in prior_pm
        in_staged = p in staged_pm
        if in_staged and not in_prior:
            added[p] = staged_pm[p]
        elif in_prior and not in_staged:
            removed[p] = prior_pm[p]
        elif prior_pm[p] != staged_pm[p]:
            changes.append({"path": p, "action": "modified"})

    removed_by_digest: StateMap = {}
    for p, d in removed.items():
        removed_by_digest.setdefault(d, []).append(p)

    for p, d in sorted(added.items()):
        if removed_by_digest.get(d):
            old_path = removed_by_digest[d].pop(0)
            changes.append({"path": p, "action": "renamed", "renamed_from": old_path})
            removed.pop(old_path, None)
        else:
            changes.append({"path": p, "action": "added"})

    for p in sorted(removed):
        changes.append({"path": p, "action": "deleted"})

    changes.sort(key=lambda c: c["path"])

    return {
        "extension": None,
        "changes": changes,
        "metadata": metadata_status_dict(store, root_path, object_id),
    }


# ---------------------------------------------------------------------------
# Content workflow primitives.
# ---------------------------------------------------------------------------


def _reverify_existing_proofs(
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
    new_job = _verify_job_from_ack(
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
            _verify_job_from_ack(
                sp.verify_batch(list(uncovered), category_id),
                uncovered,
            )
        )
        covered.update(uncovered)
    if not new_jobs:
        return state
    return replace(state, verify_jobs=(*state.verify_jobs, *new_jobs))


def _poll_outstanding_jobs(state: OtsState, sp: SimpleProofClient) -> OtsState:
    """Refresh `status` for every non-terminal verify_job."""
    jobs: list[VerifyJob] = []
    for job in state.verify_jobs:
        if _is_terminal_verify_job(job.status):
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


def _snapshot_results(
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


def _record_proof(state: OtsState, digest: str, proof: VerifiedProof) -> OtsState:
    proofs = dict(state.content_proofs)
    proofs[digest] = proof
    return replace(state, content_proofs=proofs)


def _verify_and_ingest_digest(
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
    _store_mod._atomic_write_bytes(cache_file, ots_bytes)
    ots_path_rel = f"ots-cache/{d}.ots"

    if not isinstance(result, VerifiedAttestation):
        state = _record_proof(
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
    store.save_inventory(root_path, object_id, _update_head_state(inv, head_state))
    store.save_content_map(root_path, object_id, content_map)
    inv = store.load_inventory(root_path, object_id)

    state = _record_proof(
        state,
        d,
        VerifiedProof(
            ots_path=ots_path_rel,
            verified=True,
            block_height=result.block_height,
            block_hash=result.block_hash,
            confirmations_at_verify=result.confirmations,
            verified_at=now_utc_iso(),
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
        unverified = _unverified(state, j.digests)
        if not unverified:
            continue
        poll = sp.poll_job(j.job_id)
        if poll.results_url is None:
            continue
        downloads = sp.download_results(poll.results_url, set(unverified))
        for d, ots_bytes in downloads.items():
            state, inv = _verify_and_ingest_digest(
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
            )
    return state


def _phase_advance_ready(
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
    unverified_pending = _unverified(state, state.pending_digests)
    orphans = (_non_ots_digests(final_state) - carry) - set(state.pending_digests)
    return (not unverified_pending and not orphans, unverified_pending, orphans)


def _upgrade_pass_locked(
    cfg: Config,
    store: StagingStore,
    sr,
    root_path: str,
    object_id: str,
    staging_dir: Path,
    *,
    sp_factory: SpClientFactory,
    btc_factory: BtcRpcFactory,
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
    )

    ready, unverified_pending, orphans = _phase_advance_ready(
        state, store, sr, root_path, object_id
    )
    if ready and state.phase is not Phase.CONTENT_UPGRADED:
        state = replace(state, phase=Phase.CONTENT_UPGRADED)
        save_ots(staging_dir, state)
        _log_phase(prev_phase, Phase.CONTENT_UPGRADED)
        return UpgradeContentResult(
            previous_phase=prev_phase,
            phase=state.phase,
            jobs=compute_job_progress(state),
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
        jobs=compute_job_progress(state),
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
    state = _reverify_existing_proofs(
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
    state = _poll_outstanding_jobs(state, sp)
    save_ots(staging_dir, state)
    return state


def snapshot_content_results_locked(
    state: OtsState,
    staging_dir: Path,
    *,
    sp: SimpleProofClient,
) -> OtsState:
    """Fetch mid-flight content result snapshots and persist per-job attestations."""
    return _snapshot_results(state, staging_dir, sp)


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
) -> OtsState:
    """Download completed content verify results and ingest verified proofs."""
    return _process_completed_jobs(
        state, sp, btc, store, root_path, object_id, staging_dir, min_confirmations
    )


def _upgrade_pass(
    cfg: Config,
    store: StagingStore,
    sr,
    root_path: str,
    object_id: str,
    staging_dir: Path,
    *,
    sp_factory: SpClientFactory,
    btc_factory: BtcRpcFactory,
) -> UpgradeContentResult:
    """Drive `stage timestamp content --upgrade` end-to-end under the lock."""
    with with_lock(staging_dir, timeout=_LOCK_TIMEOUT_SECONDS):
        return _upgrade_pass_locked(
            cfg,
            store,
            sr,
            root_path,
            object_id,
            staging_dir,
            sp_factory=sp_factory,
            btc_factory=btc_factory,
        )


def prepare_content_submission_locked(
    cfg: Config,
    store: StagingStore,
    sr,
    root_path: str,
    object_id: str,
    staging_dir: Path,
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
            _log_phase(state.phase, Phase.CONTENT_UPGRADED)
            return PreparedContentSubmission(
                digests=(),
                category_id=category_id,
                previous_phase=state.phase,
                target_phase=Phase.CONTENT_UPGRADED,
                noop_advanced=True,
            )
        if target_phase is not state.phase:
            save_ots(staging_dir, replace(state, phase=target_phase))
            _log_phase(state.phase, target_phase)
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
        submitted_at=now_utc_iso(),
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
    for i in range(0, len(prepared.digests), _SUBMIT_CHUNK_SIZE):
        sp.submit_hashes(
            list(prepared.digests[i : i + _SUBMIT_CHUNK_SIZE]),
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

    _log_phase(prepared.previous_phase, prepared.target_phase)
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
) -> SubmitContentResult:
    """One `stage timestamp content` submit pass as a batch of size 1."""
    prepared = prepare_content_submission_locked(
        cfg, store, sr, root_path, object_id, staging_dir
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
            submitted_at=now_utc_iso(),
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
        for j in range(0, len(digests), _SUBMIT_CHUNK_SIZE):
            sp.submit_hashes(digests[j : j + _SUBMIT_CHUNK_SIZE], category_id)

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


# ---------------------------------------------------------------------------
# Inventory workflow primitives.
# ---------------------------------------------------------------------------


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
    new_job = _verify_job_from_ack(
        sp.verify_batch([inv_digest], category_id),
        (inv_digest,),
    )
    return replace(state, inventory_proof=new_inv, verify_jobs=(*kept_jobs, new_job))


def _inventory_reverify_existing_proof(
    state: OtsState,
    btc: BitcoinRPC,
    sp: SimpleProofClient,
    staging_dir: Path,
    min_confirmations: int,
    category_id: int,
) -> OtsState:
    """Re-verify a `verified=True` ``inventory_proof.proof`` against the chain.

    Single-digest mirror of `_reverify_existing_proofs`. On mismatch /
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


def _verify_inventory_digest(
    state: OtsState,
    ots_bytes: bytes,
    btc: BitcoinRPC,
    staging_dir: Path,
    min_confirmations: int,
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
    _store_mod._atomic_write_bytes(cache_file, ots_bytes)
    ots_path_rel = f"ots-cache/{inv_digest}.ots"

    match result:
        case VerifiedAttestation(block_height=h, confirmations=c):
            new_proof = VerifiedProof(
                ots_path=ots_path_rel,
                verified=True,
                block_height=h,
                block_hash=result.block_hash,
                confirmations_at_verify=c,
                verified_at=now_utc_iso(),
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
        return _verify_inventory_digest(
            state, downloads[inv_digest], btc, staging_dir, min_confirmations
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
    state = _inventory_reverify_existing_proof(
        state, btc, sp, staging_dir, min_confirmations, category_id
    )
    save_ots(staging_dir, state)

    inv_proof = state.inventory_proof
    if inv_proof is not None and not inv_proof.proof.verified:
        inv_digest = inv_proof.digest
        has_job = any(
            inv_digest in j.digests and not _is_failed_verify_job(j.status)
            for j in state.verify_jobs
        )
        if not has_job:
            new_job = _verify_job_from_ack(
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
    state = _poll_outstanding_jobs(state, sp)
    save_ots(staging_dir, state)
    return state


def snapshot_inventory_results_locked(
    state: OtsState,
    staging_dir: Path,
    *,
    sp: SimpleProofClient,
) -> OtsState:
    """Fetch mid-flight inventory result snapshots and persist per-job attestations."""
    return _snapshot_results(state, staging_dir, sp)


def ingest_inventory_verifications_locked(
    state: OtsState,
    staging_dir: Path,
    *,
    sp: SimpleProofClient,
    btc: BitcoinRPC,
    min_confirmations: int,
) -> OtsState:
    """Download completed inventory verify results and persist proof state."""
    return _process_completed_inventory_job(
        state, sp, btc, staging_dir, min_confirmations
    )


def _inventory_upgrade_pass_locked(
    cfg: Config,
    staging_dir: Path,
    *,
    sp_factory: SpClientFactory,
    btc_factory: BtcRpcFactory,
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
    )

    if (
        state.phase is Phase.INVENTORY_UPGRADED
        and prev_phase is not Phase.INVENTORY_UPGRADED
    ):
        _log_phase(prev_phase, Phase.INVENTORY_UPGRADED)
    return UpgradeInventoryResult(
        previous_phase=prev_phase,
        phase=state.phase,
        job=compute_inventory_job_progress(state),
    )


def _inventory_upgrade_pass(
    cfg: Config,
    staging_dir: Path,
    *,
    sp_factory: SpClientFactory,
    btc_factory: BtcRpcFactory,
) -> UpgradeInventoryResult:
    """Drive `stage timestamp inventory --upgrade` end-to-end under the lock."""
    with with_lock(staging_dir, timeout=_LOCK_TIMEOUT_SECONDS):
        return _inventory_upgrade_pass_locked(
            cfg, staging_dir, sp_factory=sp_factory, btc_factory=btc_factory
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
        _require_verified_content_before_inventory(state)

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
        user=_version_user_from_staged(metadata),
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

    _log_phase(prepared.previous_phase, prepared.target_phase)
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


# ---------------------------------------------------------------------------
# Commit primitives.
# ---------------------------------------------------------------------------


def commit_bare(
    *,
    store: StagingStore,
    sr: StorageRoot,
    fs: WriteFS,
    root_path: str,
    object_id: str,
    staging_dir: Path,
    allow_missing_metadata: bool,
) -> None:
    """`stage commit` (non-OTS path): build the plan, commit it, purge staging."""
    with (
        _object_commit_lock(root_path, object_id),
        with_lock(staging_dir, timeout=_LOCK_TIMEOUT_SECONDS),
    ):
        metadata = store.load_metadata(root_path, object_id)
        require_staged_metadata(metadata, allow_missing_metadata=allow_missing_metadata)
        plan, content_source = build_inventory_plan(
            store,
            sr,
            root_path,
            object_id,
            message=metadata.message,
            user=_version_user_from_staged(metadata),
        )
        _commit_plan(sr, fs, plan, content_source)
        store.purge(root_path, object_id)


def _commit_plan(
    sr: StorageRoot, fs: WriteFS, plan: CommitPlan, content_source: ContentSource
) -> None:
    """Commit `plan`. The lib reverts a failed commit; staging is kept for retry."""
    try:
        sr.commit(fs, plan.object_id, plan, content=content_source)
    except KeyboardInterrupt:
        raise CommitFailedError(
            "commit interrupted, reverting...", interrupted=True
        ) from None
    except Exception as exc:
        raise CommitFailedError(str(exc)) from exc


def commit_ots(
    *,
    store: StagingStore,
    sr: StorageRoot,
    fs: WriteFS,
    root_path: str,
    object_id: str,
    staging_dir: Path,
    allow_missing_metadata: bool,
) -> None:
    """Attach the cached inventory proof to the timestamped plan and commit it.

    Caller catches `AttestationMismatchError` (-> exit 5) and `PhaseError`
    (-> exit 3). The staging lock is released before the commit, which
    writes the OCFL object, not staging, so a concurrent `--upgrade --watch`
    can keep running.
    """
    with _object_commit_lock(root_path, object_id):
        with with_lock(staging_dir, timeout=_LOCK_TIMEOUT_SECONDS):
            state = load_ots(staging_dir)
            target_phase = check_transition(state.phase, "commit (ots path)")
            _require_verified_content_before_inventory(state)

            if (
                state.inventory_proof is None
                or not state.inventory_proof.proof.verified
            ):
                raise AttestationMismatchError(
                    "inventory_proof missing or unverified; "
                    "re-run `stage timestamp inventory --upgrade`"
                )

            inv_digest = state.inventory_proof.digest
            plan = load_plan(staging_dir)
            if plan is None:
                raise PhaseError(
                    "inventory timestamp plan missing or stale; "
                    "run `stage timestamp inventory`"
                )
            if plan.inventory.digest != inv_digest:
                raise AttestationMismatchError(
                    f"inventory timestamp plan digest {plan.inventory.digest} "
                    f"does not match verified inventory digest {inv_digest}; "
                    "re-run `stage timestamp inventory` and "
                    "`stage timestamp inventory --upgrade`"
                )
            version = plan.inventory.inventory.versions[plan.version]
            require_timestamped_plan_metadata(
                version,
                allow_missing_metadata=allow_missing_metadata,
            )

            try:
                inv_ots = ots_cache_path(staging_dir, inv_digest).read_bytes()
            except FileNotFoundError:
                raise AttestationMismatchError(
                    f"ots-cache/{inv_digest}.ots missing; "
                    f"re-run `stage timestamp inventory --upgrade`"
                ) from None

            try:
                parsed = parse_ots(inv_ots)
            except OtsParseError as exc:
                raise AttestationMismatchError(
                    f"failed to parse cached inventory .ots ({exc}); "
                    f"re-run `stage timestamp inventory --upgrade`"
                ) from None

            if parsed.commitment_digest.hex() != inv_digest:
                raise AttestationMismatchError(
                    f"cached inventory .ots commits to "
                    f"{parsed.commitment_digest.hex()}, plan expects "
                    f"{inv_digest}; re-run `stage timestamp inventory --upgrade`"
                )

            plan = dataclasses.replace(plan, inventory_proof=inv_ots)
            content_source = _validate_and_build_content_source(
                store, root_path, object_id
            )
            prev_phase = state.phase

        _commit_plan(sr, fs, plan, content_source)

        # Purge staging on success (also removes ots-cache/, ots.json, plan.json).
        store.purge(root_path, object_id)
        _log_phase(prev_phase, target_phase)
