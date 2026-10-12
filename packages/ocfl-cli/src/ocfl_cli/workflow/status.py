"""Status use cases: what is staged, where the OTS workflow is, what to do next."""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypedDict

from ocfl_cli.model.metadata import NextActionKind
from ocfl_cli.model.phase import Phase
from ocfl_cli.store.ots_state import (
    load_ots,
)
from ocfl_cli.workflow.common import (
    StateMap,
    is_failed_verify_job,
    is_terminal_verify_job,
    non_ots_digests,
)
from ocfl_lib import (
    BitcoinRpcError,
    ots,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ocfl_cli.config import Config
    from ocfl_cli.model.ots_state import (
        OtsState,
    )
    from ocfl_cli.ots.bitcoin import (
        BlockchainInfo,
    )
    from ocfl_cli.ports import (
        BtcRpcFactory,
    )
    from ocfl_cli.store.staging import StagingStore

logger = logging.getLogger(__name__)


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
                is_failed_verify_job(j.status) and (set(j.digests) & still_pending)
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
                    is_failed_verify_job(j.status) and ip.digest in j.digests
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
        if is_terminal_verify_job(j.status):
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
        (failed_set if is_failed_verify_job(j.status) else non_failed_set).update(
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
    except BitcoinRpcError as e:
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

    non_ots = non_ots_digests(head_state)
    counts = _build_counts(state, non_ots)
    has_files = counts["total"] > 0

    kind, human = next_action_for(state, has_files)
    diff = status_diff_dict(store, root_path, object_id)

    return {
        "schema_version": 1,
        "extension": ots.EXTENSION_NAME,
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
