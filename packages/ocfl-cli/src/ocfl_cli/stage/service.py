"""Workflow orchestration for the stage command group.

Pure (typer-free) functions that coordinate the OTS workflow against
SimpleProof + Bitcoin RPC + the staging store. Typer callbacks in
`stage/commands.py` translate user options into calls here and render
returned data; watch loops in `stage/watch.py` reuse these primitives
under a ZMQ-driven block loop.
"""

import hashlib
import logging
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import filelock
from msgspec.structs import replace

from ocfl_cli.errors import (
    AttestationMismatchError,
    BitcoinError,
    CommitFailedError,
    InventoryProofMissingError,
    LockTimeoutError,
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
from ocfl_cli.stage import store as _store_mod
from ocfl_cli.stage.fsm import Phase, check_transition
from ocfl_cli.stage.ingest import ingest_file
from ocfl_cli.stage.schemas import (
    _NEXT_ACTION_HUMAN,
    NextActionKind,
    _OutstandingJobJson,
)
from ocfl_cli.stage.state import (
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
from ocfl_lib.errors import OtsParseError
from ocfl_lib.ots import parse as parse_ots
from ocfl_lib.update_plan import (
    ContentSource,
    UpdatePlan,
    deserialize,
    execute_plan,
    prepare_steps,
    revert_plan,
    serialize,
)

if TYPE_CHECKING:
    from ocfl_cli._appctx import BtcRpcFactory, SpClientFactory
    from ocfl_cli.config import Config
    from ocfl_cli.ots.simpleproof import SimpleProofClient
    from ocfl_cli.stage.store import StagingStore
    from ocfl_lib import DigestAlgorithm
    from ocfl_lib.fs.protocols import FS, WriteFS
    from ocfl_lib.inventory import Inventory


logger = logging.getLogger(__name__)

# Inventory head-state shape: digest -> logical paths.
type StateMap = dict[str, list[str]]

_SUBMIT_CHUNK_SIZE = 1000
_LOCK_TIMEOUT_SECONDS = 30
_OBJECT_LOCK_DIR = "ocfl-cli-object-locks"


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
    return {d for j in state.verify_jobs if j.status != "FAILED" for d in j.digests}


def _unverified(state: OtsState, digests: tuple[str, ...]) -> list[str]:
    """Subset of *digests* that do not yet have a verified=True proof."""
    return [
        d for d in digests if not state.content_proofs.get(d, VerifiedProof()).verified
    ]


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
                j.status == "FAILED" and (set(j.digests) & still_pending)
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
                    j.status == "FAILED" and ip.digest in j.digests
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
    from ocfl_lib.inventory import Inventory, VersionInfo

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
    user: object | None = None,
    created: str | None = None,
) -> tuple[UpdatePlan, ContentSource]:
    """Build an UpdatePlan from the staged inventory + content map.

    Used by both `stage commit` (which then prepares/executes) and
    `stage timestamp inventory` (which serializes for later commit).
    Returns the plan and the `_StagingContentSource` (commit needs it
    for `prepare_steps`).
    """
    from ocfl_lib import DigestAlgorithm
    from ocfl_lib.stage import Stage

    inv = store.load_inventory(root_path, object_id)
    alg = DigestAlgorithm.from_ocfl_name(inv.digest_algorithm)
    head = inv.head

    content_source = _validate_and_build_content_source(store, root_path, object_id)
    stage_obj = Stage(
        state=inv.versions[head].state,
        digest_algorithm=alg,
        content_source=content_source,
    )
    obj = sr.get_object(object_id)
    plan = obj.new_update_plan(
        stage_obj,
        id=object_id if not obj.exists else None,
        spec=sr.spec if not obj.exists else None,
        message=message,
        user=user,
        created=created,
    )
    return plan, content_source


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


def _build_outstanding_jobs(
    state: OtsState, now: datetime
) -> list[_OutstandingJobJson]:
    sub_by_id = {s.job_id: s for s in state.submissions if s.job_id is not None}
    outstanding: list[_OutstandingJobJson] = []
    for j in state.verify_jobs:
        if j.status in ("COMPLETED", "FAILED"):
            continue
        sub = sub_by_id.get(j.job_id)
        submitted_at = sub.submitted_at if sub is not None else ""
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
        (failed_set if j.status == "FAILED" else non_failed_set).update(j.digests)
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

    return {
        "schema_version": 1,
        "extension": "NNNN-ots-timestamps",
        "phase": state.phase.value,
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
    from ocfl_lib.digest_map import to_path_map
    from ocfl_lib.spec import VNum

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

    return {"extension": None, "changes": changes}


# ---------------------------------------------------------------------------
# Content workflow primitives.
# ---------------------------------------------------------------------------


def _reverify_existing_proofs(
    state: OtsState,
    btc: BitcoinRPC,
    sp: SimpleProofClient,
    staging_dir: Path,
    min_confirmations: int,
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
    new_job = VerifyJob(
        job_id=sp.verify_batch(list(needs_resubmit)),
        digests=tuple(needs_resubmit),
        status="IN_PROGRESS",
    )
    return replace(
        state,
        content_proofs=new_proofs,
        verify_jobs=(*kept_jobs, new_job),
    )


def _submit_new_verify_batches(state: OtsState, sp: SimpleProofClient) -> OtsState:
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
            VerifyJob(
                job_id=sp.verify_batch(list(uncovered)),
                digests=uncovered,
                status="IN_PROGRESS",
            )
        )
        covered.update(uncovered)
    if not new_jobs:
        return state
    return replace(state, verify_jobs=(*state.verify_jobs, *new_jobs))


def _poll_outstanding_jobs(state: OtsState, sp: SimpleProofClient) -> OtsState:
    """Refresh `status` for every non-terminal verify_job."""
    polled = tuple(
        j
        if j.status in ("COMPLETED", "FAILED")
        else replace(j, status=sp.poll_job(j.job_id).status)
        for j in state.verify_jobs
    )
    return replace(state, verify_jobs=polled)


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
) -> None:
    """One `--upgrade` pass. Caller MUST hold the staging lock."""
    btc_cfg = cfg.bitcoin
    if btc_cfg is None or cfg.simpleproof is None:
        raise RuntimeError("config missing required sections")
    min_confs = btc_cfg.confirmations

    state = load_ots(staging_dir)
    prev_phase = state.phase
    check_transition(state.phase, "timestamp content --upgrade")
    apply_category_id(state, cfg.simpleproof.proof_category_id)

    sp = sp_factory(cfg)
    btc = btc_factory(cfg)

    state = _reverify_existing_proofs(state, btc, sp, staging_dir, min_confs)
    save_ots(staging_dir, state)

    state = _submit_new_verify_batches(state, sp)
    save_ots(staging_dir, state)

    state = _poll_outstanding_jobs(state, sp)
    save_ots(staging_dir, state)

    state = _process_completed_jobs(
        state, sp, btc, store, root_path, object_id, staging_dir, min_confs
    )

    ready, unverified_pending, orphans = _phase_advance_ready(
        state, store, sr, root_path, object_id
    )
    if ready and state.phase is not Phase.CONTENT_UPGRADED:
        state = replace(state, phase=Phase.CONTENT_UPGRADED)
        save_ots(staging_dir, state)
        _log_phase(prev_phase, Phase.CONTENT_UPGRADED)
        return
    if unverified_pending:
        logger.warning(
            "phase advance held: %d pending digest(s) not yet verified",
            len(unverified_pending),
        )
    if orphans:
        logger.warning(
            "phase advance held: %d orphan digest(s) outside pending set",
            len(orphans),
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
) -> None:
    """Drive `stage timestamp content --upgrade` end-to-end under the lock."""
    with with_lock(staging_dir, timeout=_LOCK_TIMEOUT_SECONDS):
        _upgrade_pass_locked(
            cfg,
            store,
            sr,
            root_path,
            object_id,
            staging_dir,
            sp_factory=sp_factory,
            btc_factory=btc_factory,
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
) -> bool:
    """One `stage timestamp content` (default path) submit pass under the lock.

    Returns True when a noop-advance to CONTENT_UPGRADED occurred (so the
    caller can echo the corresponding message).
    """
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
            return True
        if target_phase is not state.phase:
            save_ots(staging_dir, replace(state, phase=target_phase))
            _log_phase(state.phase, target_phase)
        return False

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

    sp = sp_factory(cfg)
    for i in range(0, len(ordered), _SUBMIT_CHUNK_SIZE):
        sp.submit_hashes(list(ordered[i : i + _SUBMIT_CHUNK_SIZE]), category_id)

    save_ots(
        staging_dir,
        replace(
            state,
            category_id=category_id,
            submissions=(*kept, replace(intent, status="submitted")),
            pending_digests=merged_pending,
            phase=target_phase,
        ),
    )

    logger.info("submitted %d digests for %s", len(ordered), object_id)
    _log_phase(state.phase, target_phase)
    return False


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
    state: OtsState, sp: SimpleProofClient, *, ots_path: str | None
) -> OtsState:
    """Reset `inventory_proof.proof` to unverified and queue a fresh verify_batch."""
    if state.inventory_proof is None:
        return state
    inv_digest = state.inventory_proof.digest
    new_proof = VerifiedProof(ots_path=ots_path, verified=False)
    new_inv = replace(state.inventory_proof, proof=new_proof)
    kept_jobs = tuple(j for j in state.verify_jobs if inv_digest not in j.digests)
    new_job = VerifyJob(
        job_id=sp.verify_batch([inv_digest]),
        digests=(inv_digest,),
        status="IN_PROGRESS",
    )
    return replace(state, inventory_proof=new_inv, verify_jobs=(*kept_jobs, new_job))


def _inventory_reverify_existing_proof(
    state: OtsState,
    btc: BitcoinRPC,
    sp: SimpleProofClient,
    staging_dir: Path,
    min_confirmations: int,
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
        return _reset_inv_proof_and_resubmit(state, sp, ots_path=proof.ots_path)

    match result:
        case VerifiedAttestation(block_height=h, confirmations=c):
            new_proof = VerifiedProof(
                ots_path=proof.ots_path,
                verified=True,
                block_height=h,
                confirmations_at_verify=c,
                verified_at=proof.verified_at,
            )
            new_inv = replace(state.inventory_proof, proof=new_proof)
            return replace(state, inventory_proof=new_inv)
        case PendingVerification():
            return _reset_inv_proof_and_resubmit(state, sp, ots_path=proof.ots_path)


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


def _inventory_upgrade_pass_locked(
    cfg: Config,
    staging_dir: Path,
    *,
    sp_factory: SpClientFactory,
    btc_factory: BtcRpcFactory,
) -> None:
    """One inventory `--upgrade` pass. Caller MUST hold the staging lock."""
    btc_cfg = cfg.bitcoin
    if btc_cfg is None or cfg.simpleproof is None:
        raise RuntimeError("config missing required sections")
    min_confs = btc_cfg.confirmations

    state = load_ots(staging_dir)
    prev_phase = state.phase
    check_transition(state.phase, "timestamp inventory --upgrade")
    apply_category_id(state, cfg.simpleproof.proof_category_id)

    if state.inventory_proof is None:
        raise PhaseError(f"inventory_proof missing in phase {state.phase.value!r}")

    sp = sp_factory(cfg)
    btc = btc_factory(cfg)

    state = _inventory_reverify_existing_proof(state, btc, sp, staging_dir, min_confs)
    save_ots(staging_dir, state)

    inv_proof = state.inventory_proof
    if inv_proof is not None and not inv_proof.proof.verified:
        inv_digest = inv_proof.digest
        has_job = any(
            inv_digest in j.digests and j.status != "FAILED" for j in state.verify_jobs
        )
        if not has_job:
            new_job = VerifyJob(
                job_id=sp.verify_batch([inv_digest]),
                digests=(inv_digest,),
                status="IN_PROGRESS",
            )
            state = replace(state, verify_jobs=(*state.verify_jobs, new_job))
            save_ots(staging_dir, state)

    state = _poll_outstanding_jobs(state, sp)
    save_ots(staging_dir, state)

    state = _process_completed_inventory_job(state, sp, btc, staging_dir, min_confs)

    if (
        state.phase is Phase.INVENTORY_UPGRADED
        and prev_phase is not Phase.INVENTORY_UPGRADED
    ):
        _log_phase(prev_phase, Phase.INVENTORY_UPGRADED)


def _inventory_upgrade_pass(
    cfg: Config,
    staging_dir: Path,
    *,
    sp_factory: SpClientFactory,
    btc_factory: BtcRpcFactory,
) -> None:
    """Drive `stage timestamp inventory --upgrade` end-to-end under the lock."""
    with with_lock(staging_dir, timeout=_LOCK_TIMEOUT_SECONDS):
        _inventory_upgrade_pass_locked(
            cfg, staging_dir, sp_factory=sp_factory, btc_factory=btc_factory
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
) -> None:
    """One `stage timestamp inventory` (default path) submit pass under the lock."""
    sp_cfg = cfg.simpleproof
    if sp_cfg is None:
        raise RuntimeError("simpleproof config missing")
    state = load_ots(staging_dir)
    target_phase = check_transition(state.phase, "timestamp inventory")
    category_id = apply_category_id(state, sp_cfg.proof_category_id)

    if state.phase is Phase.INVENTORY_UPGRADED:
        return

    # On resubmit, preserve `created` from the prior plan so the new
    # inventory digest stays identical across retries.
    prior_created: str | None = None
    if state.inventory_proof is not None and plan_path(staging_dir).is_file():
        prior_plan = deserialize(plan_path(staging_dir).read_bytes())
        prior_inv = prior_plan.new_inv.inventory
        prior_created = prior_inv.versions[prior_inv.head].created

    plan, _cs = build_inventory_plan(
        store, sr, root_path, object_id, created=prior_created
    )
    inv_digest = plan.new_inv.digest
    head_version = plan.new_inv.inventory.head

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

    sp = sp_factory(cfg)
    sp.submit_hashes([inv_digest], category_id)

    plan_path(staging_dir).write_bytes(serialize(plan))

    _log_phase(prev_phase, target_phase)
    logger.info("submitted inventory digest %s for %s", inv_digest, object_id)


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
    sr,
    fs: WriteFS,
    obj_dir: str,
    root_path: str,
    object_id: str,
    message: str | None,
    user: object | None,
    created: str | None,
) -> None:
    """`stage commit` (non-OTS path): build plan, prepare, execute, purge."""
    with _object_commit_lock(root_path, object_id):
        plan, content_source = build_inventory_plan(
            store,
            sr,
            root_path,
            object_id,
            message=message,
            user=user,
            created=created,
        )
        prepare_steps(plan, fs, obj_dir, content_source)

        # Transactional boundary: execute_plan failure -> revert + preserve staging
        # for retry. revert_plan failures propagate as-is.
        try:
            execute_plan(plan)
        except KeyboardInterrupt:
            revert_plan(plan)
            raise CommitFailedError(
                "commit interrupted, reverting...", interrupted=True
            ) from None
        except Exception as exc:
            revert_plan(plan)
            raise CommitFailedError(str(exc)) from exc

        store.purge(root_path, object_id)


def commit_ots(
    *,
    store: StagingStore,
    fs: WriteFS,
    obj_dir: str,
    root_path: str,
    object_id: str,
    staging_dir: Path,
) -> None:
    """Complete deferred extension_files from cache, prepare + execute plan.

    Caller catches `AttestationMismatchError` (-> exit 5) and `PhaseError`
    (-> exit 3). The staging lock is released before plan execution:
    `prepare_steps` / `execute_plan` / `revert_plan` mutate the OCFL
    object directory, not staging, so a concurrent `--upgrade --watch`
    can keep running.
    """
    with _object_commit_lock(root_path, object_id):
        with with_lock(staging_dir, timeout=_LOCK_TIMEOUT_SECONDS):
            state = load_ots(staging_dir)
            target_phase = check_transition(state.phase, "commit (ots path)")

            if (
                state.inventory_proof is None
                or not state.inventory_proof.proof.verified
            ):
                raise AttestationMismatchError(
                    "inventory_proof missing or unverified; "
                    "re-run `stage timestamp inventory --upgrade`"
                )

            inv_digest = state.inventory_proof.digest
            plan = deserialize(plan_path(staging_dir).read_bytes())

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

            head = plan.new_inv.inventory.head
            plan.extension_files[
                f"extensions/NNNN-ots-timestamps/inventory.json.{head}.ots"
            ] = inv_ots

            content_source = _validate_and_build_content_source(
                store, root_path, object_id
            )
            prev_phase = state.phase

        prepare_steps(plan, fs, obj_dir, content_source)
        # Transactional boundary: execute_plan failure -> revert + preserve staging
        # for retry. revert_plan failures propagate as-is.
        try:
            execute_plan(plan)
        except KeyboardInterrupt:
            revert_plan(plan)
            raise CommitFailedError(
                "commit interrupted, reverting...", interrupted=True
            ) from None
        except Exception as exc:
            revert_plan(plan)
            raise CommitFailedError(str(exc)) from exc

        # Purge staging on success (also removes ots-cache/, ots.json, plan.json).
        store.purge(root_path, object_id)
        _log_phase(prev_phase, target_phase)
