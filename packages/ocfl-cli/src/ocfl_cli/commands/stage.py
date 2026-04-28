"""ocfl stage command group."""

import base64
import contextlib
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, TypedDict, cast

import httpx
import typer
from platformdirs import user_state_path
from rich.text import Text

from ocfl_cli import _style
from ocfl_cli._common import err, open_root
from ocfl_cli.errors import (
    AttestationMismatchError,
    BitcoinError,
    LockTimeoutError,
    PhaseError,
    ReorgDetectedError,
)
from ocfl_cli.ingest import ingest_file
from ocfl_cli.ots.bitcoin import (
    BitcoinRPC,
    BitcoinRPCClient,
    BlockchainInfo,
    HashBlockStream,
    PendingVerification,
    VerifiedAttestation,
    verify_attestation,
)
from ocfl_cli.ots.simpleproof import SimpleProofCreds, SimpleProofHttpClient
from ocfl_cli.stage import store as _store_mod
from ocfl_cli.stage.fsm import Phase, check_transition
from ocfl_cli.stage.state import (
    InventoryProof,
    Submission,
    VerifiedProof,
    VerifyJob,
    apply_category_id,
    has_ots,
    load_ots,
    now_utc_iso,
    ots_cache_path,
    plan_path,
    save_ots,
    with_lock,
)
from ocfl_cli.stage.store import StagingStore
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
    from collections.abc import Callable

    from ocfl_cli.config import Config, SimpleProofConfig
    from ocfl_cli.ots.simpleproof import SimpleProofClient
    from ocfl_cli.stage.state import OtsState
    from ocfl_lib import DigestAlgorithm
    from ocfl_lib.fs.protocols import FS, WriteFS
    from ocfl_lib.inventory import Inventory


logger = logging.getLogger(__name__)

# Inventory head-state shape: digest -> logical paths.
type StateMap = dict[str, list[str]]

_SUBMIT_CHUNK_SIZE = 1000
_LOCK_TIMEOUT_SECONDS = 30

stage_app = typer.Typer(
    name="stage",
    help="Stage changes for OCFL objects.",
    no_args_is_help=True,
)


def _resolve_root_path(ctx: typer.Context) -> str:
    """Resolve absolute root path string for staging path hashing."""
    return str(Path(ctx.obj.get("root") or Path.cwd()).resolve())


def _require_ots_config(cfg: Config | None, digest: str | None) -> SimpleProofConfig:
    """Preflight `stage new --ots`. Returns validated [simpleproof] config."""
    if digest is not None and digest != "sha256":
        err(
            f"--ots requires SHA-256 (SimpleProof API constraint); "
            f"got --digest={digest!r}"
        )
        raise typer.Exit(2)
    where = (
        f"in {cfg.path}"
        if cfg is not None and cfg.path is not None
        else "in your ocfl config"
    )
    if cfg is None or cfg.simpleproof is None:
        err(f"--ots requires [simpleproof] section {where}")
        raise typer.Exit(2)
    if cfg.bitcoin is None:
        err(f"--ots requires [bitcoin] section {where}")
        raise typer.Exit(2)
    return cfg.simpleproof


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


@stage_app.callback()
def stage_callback(
    ctx: typer.Context,
    staging_root: Annotated[
        str | None,
        typer.Option(
            "--staging-root",
            envvar="OCFL_STAGING_ROOT",
            help="Path to staging directory.",
        ),
    ] = None,
) -> None:
    """Stage changes for OCFL objects."""
    ctx.ensure_object(dict)
    if staging_root:
        root = Path(staging_root)
    else:
        root = user_state_path("ocfl-cli") / "staging"
    root.mkdir(parents=True, exist_ok=True)
    ctx.obj["staging"] = StagingStore(root)


@stage_app.command()
def new(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID to stage.")],
    digest: Annotated[
        str | None,
        typer.Option("--digest", help="Digest algorithm (new objects only)."),
    ] = None,
    ots: Annotated[
        bool,
        typer.Option(
            "--ots",
            help="Enable OpenTimestamps workflow (requires SHA-256).",
        ),
    ] = False,
) -> None:
    """Create a new staging area for an object."""
    from ocfl_cli.stage.state import OtsState
    from ocfl_lib import DigestAlgorithm
    from ocfl_lib.inventory_builder import InventoryBuilder

    cfg = ctx.obj.get("config")
    sp_cfg: SimpleProofConfig | None = None
    if ots:
        sp_cfg = _require_ots_config(cfg, digest)
        digest = "sha256"

    store: StagingStore = ctx.obj["staging"]
    sr, _fs = open_root(ctx.obj.get("root"))
    root_path = _resolve_root_path(ctx)

    if store.exists(root_path, object_id):
        err(f"staging already exists for {object_id!r}")
        raise typer.Exit(1)

    obj = sr.get_object(object_id)
    if obj.exists:
        # Existing object: clone head inventory, add empty v(N+1)
        head_inv = obj.inventory.inventory
        builder = InventoryBuilder(head_inv).add_version(
            {}, created="1970-01-01T00:00:00Z"
        )
        inv = builder.build()
    else:
        # New object: build empty v1 inventory
        alg = DigestAlgorithm.SHA512
        digest_name = digest
        if digest_name is None and cfg is not None:
            digest_name = cfg.effective_digest(ctx.obj.get("archive"))
        if digest_name:
            try:
                alg = DigestAlgorithm.from_ocfl_name(digest_name)
            except KeyError, ValueError:
                err(f"unknown digest algorithm: {digest_name!r}")
                raise typer.Exit(1) from None

        inv = (
            InventoryBuilder()
            .id(object_id)
            .spec(sr.spec)
            .digest_algorithm(alg)
            .add_version({}, created="1970-01-01T00:00:00Z")
            .build()
        )

    store.create(root_path, object_id, inv)

    if sp_cfg is not None:
        save_ots(
            store.staging_path(root_path, object_id),
            OtsState(phase=Phase.DRAFTING, category_id=sp_cfg.proof_category_id),
        )


@stage_app.command()
def add(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    src: Annotated[list[str], typer.Argument(help="Source file(s) or directory(ies).")],
    as_path: Annotated[
        str | None,
        typer.Option("--as", help="Logical path (file) or prefix (dir)."),
    ] = None,
) -> None:
    """Add files to a staged object version."""
    from ocfl_cli.ingest import ingest_file
    from ocfl_lib import DigestAlgorithm

    store: StagingStore = ctx.obj["staging"]
    root_path = _resolve_root_path(ctx)

    if not store.exists(root_path, object_id):
        err(f"no staging exists for {object_id!r} (run 'stage new' first)")
        raise typer.Exit(1)

    inv = store.load_inventory(root_path, object_id)
    alg = DigestAlgorithm.from_ocfl_name(inv.digest_algorithm)
    head = inv.head
    state = dict(inv.versions[head].state)
    content_map = store.load_content_map(root_path, object_id)
    staging_dir = store.staging_path(root_path, object_id)

    path_to_digest: dict[str, str] = {}
    for d, paths in state.items():
        for p in paths:
            path_to_digest[p] = d

    prior_digests = set(inv.manifest.keys())

    def _add_file(file_path: Path, logical_path: str) -> None:
        result = ingest_file(
            file_path,
            logical_path,
            staging_dir=staging_dir,
            content_map=content_map,
            digest_alg=alg,
        )
        d = result.digest

        if d in prior_digests:
            content_map.pop(d, None)

        if logical_path in path_to_digest:
            old_d = path_to_digest[logical_path]
            old_paths = state.get(old_d, [])
            if logical_path in old_paths:
                old_paths.remove(logical_path)
                if not old_paths:
                    state.pop(old_d, None)

        if d in state:
            if logical_path not in state[d]:
                state[d].append(logical_path)
        else:
            state[d] = [logical_path]
        path_to_digest[logical_path] = d

    for s in src:
        p = Path(s)
        if p.is_file() and not p.is_symlink():
            logical = as_path or p.name
            _add_file(p, logical)
        elif p.is_dir():
            for child in sorted(p.rglob("*")):
                if child.is_symlink():
                    continue
                if not child.is_file():
                    continue
                rel = str(child.relative_to(p))
                logical = f"{as_path}/{rel}" if as_path else rel
                _add_file(child, logical)

    store.save_inventory(root_path, object_id, _update_head_state(inv, state))
    store.save_content_map(root_path, object_id, content_map)


def _pair_ots_companion_remove(
    state: StateMap, removed_paths: list[str]
) -> None:
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


@stage_app.command()
def mv(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    src_path: Annotated[str, typer.Argument(help="Source logical path.")],
    dst_path: Annotated[str, typer.Argument(help="Destination logical path.")],
) -> None:
    """Rename a logical path in the staged version."""
    store: StagingStore = ctx.obj["staging"]
    root_path = _resolve_root_path(ctx)

    if not store.exists(root_path, object_id):
        err(f"no staging exists for {object_id!r}")
        raise typer.Exit(1)

    inv = store.load_inventory(root_path, object_id)
    state = {d: list(ps) for d, ps in inv.versions[inv.head].state.items()}

    # Find which digest owns src_path
    found_digest = None
    for d, paths in state.items():
        if src_path in paths:
            found_digest = d
            break

    if found_digest is None:
        err(f"path not found: {src_path!r}")
        raise typer.Exit(1)

    # Rename: remove src, add dst
    state[found_digest].remove(src_path)
    state[found_digest].append(dst_path)

    staging_dir = store.staging_path(root_path, object_id)

    if has_ots(staging_dir):
        with with_lock(staging_dir, timeout=_LOCK_TIMEOUT_SECONDS):
            _pair_ots_companion_rename(state, src_path, dst_path)
            store.save_inventory(root_path, object_id, _update_head_state(inv, state))
            _apply_rm_mv_regression(
                staging_dir, state, "mv", f"stage mv {src_path} {dst_path}"
            )
    else:
        store.save_inventory(root_path, object_id, _update_head_state(inv, state))


@stage_app.command()
def rm(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    paths: Annotated[list[str], typer.Argument(help="Logical path(s) to remove.")],
    recursive: Annotated[
        bool, typer.Option("-r", help="Remove all paths with matching prefix.")
    ] = False,
) -> None:
    """Remove logical paths from the staged version."""
    store: StagingStore = ctx.obj["staging"]
    root_path = _resolve_root_path(ctx)

    if not store.exists(root_path, object_id):
        err(f"no staging exists for {object_id!r}")
        raise typer.Exit(1)

    inv = store.load_inventory(root_path, object_id)
    state = {d: list(ps) for d, ps in inv.versions[inv.head].state.items()}
    content_map = store.load_content_map(root_path, object_id)
    staging_dir = store.staging_path(root_path, object_id)

    removed_paths: list[str] = []
    for target in paths:
        if recursive:
            prefix = target.rstrip("/") + "/"
            for d in list(state):
                kept: list[str] = []
                for p in state[d]:
                    if p == target or p.startswith(prefix):
                        removed_paths.append(p)
                    else:
                        kept.append(p)
                if kept:
                    state[d] = kept
                else:
                    del state[d]
        else:
            for d in list(state):
                if target in state[d]:
                    state[d].remove(target)
                    if not state[d]:
                        del state[d]
                    removed_paths.append(target)
                    break
            else:
                err(f"path not found: {target!r}")
                raise typer.Exit(1)

    if not removed_paths:
        err("no matching paths found")
        raise typer.Exit(1)

    def _finalize() -> None:
        referenced_digests = set(state.keys())
        for d in list(content_map):
            if d not in referenced_digests:
                del content_map[d]
        store.save_inventory(root_path, object_id, _update_head_state(inv, state))
        store.save_content_map(root_path, object_id, content_map)

    if has_ots(staging_dir):
        with with_lock(staging_dir, timeout=_LOCK_TIMEOUT_SECONDS):
            _pair_ots_companion_remove(state, removed_paths)
            _finalize()
            _apply_rm_mv_regression(
                staging_dir, state, "rm", f"stage rm {' '.join(paths)}"
            )
    else:
        _finalize()


@stage_app.command("ls")
def ls_cmd(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    digests: Annotated[
        bool, typer.Option("--digests", help="Show digests alongside paths.")
    ] = False,
) -> None:
    """List files in the staged head version."""
    store: StagingStore = ctx.obj["staging"]
    root_path = _resolve_root_path(ctx)

    if not store.exists(root_path, object_id):
        err(f"no staging exists for {object_id!r}")
        raise typer.Exit(1)

    inv = store.load_inventory(root_path, object_id)
    state = inv.versions[inv.head].state

    # Collect (path, digest) pairs sorted by path
    entries: list[tuple[str, str]] = []
    for d, paths in state.items():
        for p in paths:
            entries.append((p, d))
    entries.sort()

    if ctx.obj.get("json"):
        from ocfl_cli._common import output_json

        if digests:
            output_json([{"path": p, "digest": d} for p, d in entries])
        else:
            output_json([p for p, _ in entries])
    else:
        for p, d in entries:
            if digests:
                _style.echo(Text.assemble(_style.digest(d), "  ", p))
            else:
                typer.echo(p)


class NextActionKind(StrEnum):
    """Stable enum surfaced as `next_action_kind` in `stage status --json`.

    Scripts MUST tolerate unknown values (forward-compat).
    """

    TIMESTAMP_CONTENT = "timestamp_content"
    UPGRADE_CONTENT = "upgrade_content"
    RETRY_CONTENT = "retry_content"
    TIMESTAMP_INVENTORY = "timestamp_inventory"
    UPGRADE_INVENTORY = "upgrade_inventory"
    RETRY_INVENTORY = "retry_inventory"
    COMMIT = "commit"
    ADD_CONTENT = "add_content"
    NONE = "none"


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


@stage_app.command()
def status(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
) -> None:
    """Show changes in staged version vs committed head (OTS-aware)."""
    store: StagingStore = ctx.obj["staging"]
    root_path = _resolve_root_path(ctx)

    if not store.exists(root_path, object_id):
        err(f"no staging exists for {object_id!r}")
        raise typer.Exit(1)

    staging_dir = store.staging_path(root_path, object_id)
    if has_ots(staging_dir):
        _status_ots(ctx, store, root_path, object_id, staging_dir)
    else:
        _status_diff(ctx, store, root_path, object_id)


def _status_diff(
    ctx: typer.Context,
    store: StagingStore,
    root_path: str,
    object_id: str,
) -> None:
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

    if ctx.obj.get("json"):
        from ocfl_cli._common import output_json

        output_json({"extension": None, "changes": changes})
    else:
        action_prefix = {"added": "A", "deleted": "D", "modified": "M", "renamed": "R"}
        sym_style = {
            "A": _style.added,
            "D": _style.deleted,
            "M": _style.modified,
            "R": _style.renamed,
        }
        for c in changes:
            prefix = action_prefix.get(c["action"], "?")
            styler = sym_style.get(prefix)
            styled_sym = styler(prefix) if styler else Text(prefix)
            parts: list = [styled_sym, " ", c["path"]]
            if c.get("renamed_from"):
                parts.append(f" (from {c['renamed_from']})")
            _style.echo(Text.assemble(*parts))


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


def _build_counts(
    state: OtsState, non_ots: set[str]
) -> tuple[dict[str, int], int, int]:
    total = len(non_ots)
    upgraded_set = {d for d, p in state.content_proofs.items() if p.verified}
    upgraded = len(upgraded_set & non_ots) if non_ots else 0
    pending = total - upgraded

    non_failed_set: set[str] = set()
    failed_set: set[str] = set()
    for j in state.verify_jobs:
        (failed_set if j.status == "FAILED" else non_failed_set).update(j.digests)
    submitted = len(non_failed_set)
    failed = len(failed_set - non_failed_set)

    counts = {
        "total": total,
        "submitted": submitted,
        "upgraded": upgraded,
        "pending": pending,
        "failed": failed,
    }
    return counts, upgraded, pending


def _build_bitcoin_section(cfg: Config | None) -> dict[str, object] | None:
    if cfg is None or cfg.bitcoin is None:
        return None
    try:
        info = _get_btc_rpc(cfg).getblockchaininfo()
    except BitcoinError as e:
        logger.warning("bitcoin RPC unreachable, omitting bitcoin section: %s", e)
        return None
    return {
        "height": info.height,
        "tip_hash": info.best_block_hash,
        "in_sync": compute_in_sync(info),
        "confirmations_required": cfg.bitcoin.confirmations,
    }


def _status_ots(
    ctx: typer.Context,
    store: StagingStore,
    root_path: str,
    object_id: str,
    staging_dir: Path,
) -> None:
    cfg: Config | None = ctx.obj.get("config")
    state = load_ots(staging_dir)
    inv = store.load_inventory(root_path, object_id)
    head_state = inv.versions[inv.head].state

    non_ots = _non_ots_digests(head_state)
    counts, upgraded, pending = _build_counts(state, non_ots)
    failed = counts["failed"]
    total = counts["total"]
    has_files = total > 0

    outstanding = _build_outstanding_jobs(state, _status_now())
    bitcoin = _build_bitcoin_section(cfg)
    simpleproof = {
        "configured": cfg is not None and cfg.simpleproof is not None,
        "category_id": state.category_id,
    }
    cache_bytes = _cache_bytes(staging_dir)
    kind, human = next_action_for(state, has_files)

    if ctx.obj.get("json"):
        from ocfl_cli._common import output_json

        output_json(
            {
                "schema_version": 1,
                "extension": "NNNN-ots-timestamps",
                "phase": state.phase.value,
                "counts": counts,
                "outstanding_jobs": outstanding,
                "bitcoin": bitcoin,
                "simpleproof": simpleproof,
                "cache_bytes": cache_bytes,
                "next_action_kind": kind.value,
                "next_action": human,
            }
        )
        return

    parts = [f"Phase: {state.phase.value} ({upgraded} of {total} upgraded"]
    if pending:
        parts.append(f", {pending} outstanding")
    if failed:
        parts.append(f", {failed} failed")
    parts.append(")")
    typer.echo("".join(parts))

    if outstanding:
        typer.echo("Outstanding:")
        for o in outstanding:
            short = f"{o['digests'][0][:12]}..." if o["digests"] else ""
            typer.echo(
                f"  sha256:{short} — job {o['job_id']}, {o['status']}, "
                f"submitted {humanize_age(o['age_seconds'])}"
            )

    if bitcoin is not None:
        sync_str = "node in sync" if bitcoin["in_sync"] else "node not in sync"
        typer.echo(f"Bitcoin: height {bitcoin['height']} ({sync_str})")

    typer.echo(
        "SimpleProof: configured"
        if simpleproof["configured"]
        else "SimpleProof: not configured"
    )
    typer.echo(f"Next: {human}")


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
            err(f"source file missing: {file_path}")
            raise typer.Exit(1)
        stat = file_path.stat()
        if stat.st_size != entry["size"] or stat.st_mtime != entry["mtime"]:
            err(f"source file changed since staging: {file_path}")
            raise typer.Exit(1)
    return _StagingContentSource(content_map)


def _build_inventory_plan(
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


@stage_app.command()
def commit(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    message: Annotated[
        str | None, typer.Option("-m", "--message", help="Version message.")
    ] = None,
    name: Annotated[str | None, typer.Option("--name", help="Creator name.")] = None,
    address: Annotated[
        str | None, typer.Option("--address", help="Creator address.")
    ] = None,
    created: Annotated[
        str | None, typer.Option("--created", help="Created timestamp (ISO 8601).")
    ] = None,
) -> None:
    """Commit staged changes to the storage root."""
    from ocfl_lib.inventory import UserInfo

    store: StagingStore = ctx.obj["staging"]
    sr, fs = open_root(ctx.obj.get("root"))
    root_path = _resolve_root_path(ctx)

    if not store.exists(root_path, object_id):
        err(f"no staging exists for {object_id!r}")
        raise typer.Exit(1)

    staging_dir = store.staging_path(root_path, object_id)
    obj = sr.get_object(object_id)

    # OTS commit path: phase=INVENTORY_UPGRADED + serialized plan on disk.
    # Both conditions required (a partial state means the OTS workflow has
    # not reached the deferred-completion point yet).
    if has_ots(staging_dir) and plan_path(staging_dir).is_file():
        try:
            _commit_ots_path(
                store=store,
                fs=fs,
                obj_dir=obj._path,
                root_path=root_path,
                object_id=object_id,
                staging_dir=staging_dir,
            )
        except AttestationMismatchError as e:
            err(str(e))
            raise typer.Exit(5) from None
        return

    user = None
    if name:
        user = UserInfo(name=name, address=address)

    plan, content_source = _build_inventory_plan(
        store,
        sr,
        root_path,
        object_id,
        message=message,
        user=user,
        created=created,
    )
    prepare_steps(plan, fs, obj._path, content_source)

    try:
        execute_plan(plan)
    except KeyboardInterrupt:
        err("commit interrupted, reverting...")
        revert_plan(plan)
        raise typer.Exit(130) from None
    except Exception as exc:
        err(str(exc))
        revert_plan(plan)
        raise typer.Exit(1) from None

    # Purge staging on success
    store.purge(root_path, object_id)


def _commit_ots_path(
    *,
    store: StagingStore,
    fs: WriteFS,
    obj_dir: str,
    root_path: str,
    object_id: str,
    staging_dir: Path,
) -> None:
    """Complete deferred extension_files from cache, prepare + execute plan.

    Caller catches `AttestationMismatchError` and maps to exit 5. The
    staging lock is released before plan execution: `prepare_steps` /
    `execute_plan` / `revert_plan` mutate the OCFL object directory,
    not staging, so a concurrent `--upgrade --watch` can keep running.
    """
    with with_lock(staging_dir, timeout=_LOCK_TIMEOUT_SECONDS):
        state = load_ots(staging_dir)
        try:
            target_phase = check_transition(state.phase, "commit (ots path)")
        except PhaseError as e:
            err(
                f"{e}; run 'stage timestamp inventory --upgrade' to advance "
                f"to phase=inventory-upgraded"
            )
            raise typer.Exit(3) from None

        if state.inventory_proof is None or not state.inventory_proof.proof.verified:
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

        content_source = _validate_and_build_content_source(store, root_path, object_id)
        prev_phase = state.phase

    prepare_steps(plan, fs, obj_dir, content_source)
    try:
        execute_plan(plan)
    except KeyboardInterrupt:
        err("commit interrupted, reverting...")
        revert_plan(plan)
        raise typer.Exit(130) from None
    except Exception as exc:
        err(str(exc))
        revert_plan(plan)
        raise typer.Exit(1) from None

    # Purge staging on success (also removes ots-cache/, ots.json, plan.json).
    store.purge(root_path, object_id)
    _log_phase(prev_phase, target_phase)


timestamp_app = typer.Typer(
    name="timestamp",
    help="Timestamp staged content / inventory via SimpleProof.",
    no_args_is_help=True,
)
stage_app.add_typer(timestamp_app)


def _get_sp_client(
    cfg: Config,
    sleep: Callable[[float], None] = time.sleep,
) -> SimpleProofClient:
    """Construct the production `SimpleProofClient` from `cfg`.

    Tests monkeypatch this seam to inject `FakeSimpleProof`.
    """
    sp_cfg = cfg.simpleproof
    if sp_cfg is None:
        raise RuntimeError("simpleproof config missing")
    client = httpx.Client(timeout=sp_cfg.timeout_s)
    creds = SimpleProofCreds(
        access_token=sp_cfg.access_token.get_secret_value(),
        api_key=sp_cfg.api_key.get_secret_value(),
    )
    return SimpleProofHttpClient(sp_cfg.base_url, creds, client=client, sleep=sleep)


def _get_btc_rpc(cfg: Config) -> BitcoinRPC:
    """Construct the production `BitcoinRPCClient` from `cfg`.

    Tests monkeypatch this seam to inject `FakeBitcoinRPC`.
    """
    btc_cfg = cfg.bitcoin
    if btc_cfg is None:
        raise RuntimeError("bitcoin config missing")

    creds = f"{btc_cfg.rpc_user}:{btc_cfg.rpc_password.get_secret_value()}"
    headers = {"Authorization": "Basic " + base64.b64encode(creds.encode()).decode()}
    client = httpx.Client(timeout=btc_cfg.rpc_timeout_s, headers=headers)
    return BitcoinRPCClient(btc_cfg.rpc_url, client=client)


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


def _unverified(state: OtsState, digests: tuple[str, ...]) -> list[str]:
    """Subset of *digests* that do not yet have a verified=True proof."""
    return [
        d for d in digests if not state.content_proofs.get(d, VerifiedProof()).verified
    ]


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


def _record_proof(state: OtsState, digest: str, proof: VerifiedProof) -> OtsState:
    proofs = dict(state.content_proofs)
    proofs[digest] = proof
    return replace(state, content_proofs=proofs)


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

    sp = _get_sp_client(cfg)
    btc = _get_btc_rpc(cfg)

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
) -> None:
    """Drive `stage timestamp content --upgrade` end-to-end under the lock."""
    with with_lock(staging_dir, timeout=_LOCK_TIMEOUT_SECONDS):
        _upgrade_pass_locked(cfg, store, sr, root_path, object_id, staging_dir)


def _get_zmq_subscriber(cfg: Config) -> HashBlockStream:
    """Construct the production ``BitcoinZmqSubscriber`` from `cfg`.

    Tests monkeypatch this seam to inject ``InMemoryHashBlockStream``.
    """
    from ocfl_cli.ots.bitcoin import BitcoinZmqSubscriber

    btc_cfg = cfg.bitcoin
    if btc_cfg is None:
        raise RuntimeError("bitcoin config missing")
    return BitcoinZmqSubscriber(btc_cfg.zmq_endpoint)


def _watch_loop(
    cfg: Config,
    store: StagingStore,
    sr,
    root_path: str,
    object_id: str,
    staging_dir: Path,
) -> int:
    """Drive ``--upgrade --watch`` end-to-end. Returns exit code."""
    btc_cfg = cfg.bitcoin
    if btc_cfg is None or cfg.simpleproof is None:
        raise RuntimeError("config missing required sections")
    min_confs = btc_cfg.confirmations
    btc = _get_btc_rpc(cfg)
    last_height: int | None = None
    last_tip_hash: str | None = None
    stream = _get_zmq_subscriber(cfg)
    # Real BitcoinZmqSubscriber is a context manager (closes the socket on
    # exit); test fakes implement only `wait_for_block`. nullcontext bridges
    # both with one `with` site.
    stream_cm = cast(
        "contextlib.AbstractContextManager[HashBlockStream]",
        stream if hasattr(stream, "__enter__") else contextlib.nullcontext(stream),
    )
    try:
        with stream_cm as zmq_sub:
            while True:
                block = zmq_sub.wait_for_block(timeout_ms=5000)
                if block is None:
                    continue
                try:
                    with with_lock(staging_dir, timeout=_LOCK_TIMEOUT_SECONDS):
                        info = btc.getblockchaininfo()
                        height_jumped = (
                            last_height is not None and info.height - last_height > 1
                        )
                        tip_changed_at_stable = (
                            last_height is not None
                            and info.height == last_height
                            and last_tip_hash is not None
                            and last_tip_hash != info.best_block_hash
                        )
                        if height_jumped or tip_changed_at_stable:
                            sp = _get_sp_client(cfg)
                            state = load_ots(staging_dir)
                            state = _reverify_existing_proofs(
                                state, btc, sp, staging_dir, min_confs
                            )
                            save_ots(staging_dir, state)
                        last_height = info.height
                        last_tip_hash = info.best_block_hash
                        _upgrade_pass_locked(
                            cfg, store, sr, root_path, object_id, staging_dir
                        )
                        state = load_ots(staging_dir)
                        if state.phase is Phase.CONTENT_UPGRADED:
                            return 0
                except LockTimeoutError as e:
                    logger.warning(
                        "staging lock contended (%s); retrying on next block", e
                    )
                    continue
    except KeyboardInterrupt:
        return 130


@timestamp_app.command("content")
def timestamp_content(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    upgrade: Annotated[
        bool,
        typer.Option(
            "--upgrade",
            help="Poll SP, verify .ots against Bitcoin, ingest companion files.",
        ),
    ] = False,
    watch: Annotated[
        bool,
        typer.Option(
            "--watch",
            help="ZMQ-driven loop; runs an upgrade pass per Bitcoin block.",
        ),
    ] = False,
    retry: Annotated[
        bool,
        typer.Option("--retry", help="Resubmit FAILED submissions only."),
    ] = False,
) -> None:
    """Submit staged content digests to SimpleProof."""
    cfg: Config | None = ctx.obj.get("config")
    if cfg is None or cfg.simpleproof is None:
        err("'stage timestamp content' requires [simpleproof] config section")
        raise typer.Exit(2)
    if retry:
        for other_name, other_set in (("--upgrade", upgrade), ("--watch", watch)):
            if other_set:
                err(
                    "'stage timestamp content --retry' is incompatible with "
                    f"{other_name}"
                )
                raise typer.Exit(2)
    if upgrade and cfg.bitcoin is None:
        err("'stage timestamp content --upgrade' requires [bitcoin] config section")
        raise typer.Exit(2)
    if watch and not upgrade:
        err("'stage timestamp content --watch' requires --upgrade")
        raise typer.Exit(2)

    store: StagingStore = ctx.obj["staging"]
    sr, _fs = open_root(ctx.obj.get("root"))
    root_path = _resolve_root_path(ctx)

    if not store.exists(root_path, object_id):
        err(f"no staging exists for {object_id!r}")
        raise typer.Exit(1)

    staging_dir = store.staging_path(root_path, object_id)
    if not has_ots(staging_dir):
        err(f"{object_id!r} is not an OTS staging (run 'stage new --ots' first)")
        raise typer.Exit(2)

    if watch:
        try:
            exit_code = _watch_loop(cfg, store, sr, root_path, object_id, staging_dir)
        except (ReorgDetectedError, AttestationMismatchError) as e:
            err(f"halt: {e}")
            raise typer.Exit(5) from None
        raise typer.Exit(exit_code)

    if upgrade:
        try:
            _upgrade_pass(cfg, store, sr, root_path, object_id, staging_dir)
        except (ReorgDetectedError, AttestationMismatchError) as e:
            err(f"halt: {e}")
            raise typer.Exit(5) from None
        return

    if retry:
        with with_lock(staging_dir, timeout=_LOCK_TIMEOUT_SECONDS):
            state = load_ots(staging_dir)
            try:
                target_phase = check_transition(
                    state.phase, "timestamp content --retry"
                )
            except PhaseError as e:
                err(str(e))
                raise typer.Exit(3) from None
            category_id = apply_category_id(state, cfg.simpleproof.proof_category_id)

            failed_indices = [
                i for i, s in enumerate(state.submissions) if s.status == "failed"
            ]
            if not failed_indices:
                typer.echo("no failed submissions to retry")
                return

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

            sp = _get_sp_client(cfg)
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
        return

    with with_lock(staging_dir, timeout=_LOCK_TIMEOUT_SECONDS):
        state = load_ots(staging_dir)
        target_phase = check_transition(state.phase, "timestamp content")
        category_id = apply_category_id(state, cfg.simpleproof.proof_category_id)

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
                typer.echo("nothing to timestamp; advancing to content-upgraded")
                _log_phase(state.phase, Phase.CONTENT_UPGRADED)
            elif target_phase is not state.phase:
                save_ots(staging_dir, replace(state, phase=target_phase))
                _log_phase(state.phase, target_phase)
            return

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

        sp = _get_sp_client(cfg)
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


def _inventory_upgrade_pass_locked(cfg: Config, staging_dir: Path) -> None:
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

    sp = _get_sp_client(cfg)
    btc = _get_btc_rpc(cfg)

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


def _inventory_upgrade_pass(cfg: Config, staging_dir: Path) -> None:
    """Drive `stage timestamp inventory --upgrade` end-to-end under the lock."""
    with with_lock(staging_dir, timeout=_LOCK_TIMEOUT_SECONDS):
        _inventory_upgrade_pass_locked(cfg, staging_dir)


def _inventory_watch_loop(cfg: Config, staging_dir: Path) -> int:
    """Drive `stage timestamp inventory --upgrade --watch`. Returns exit code."""
    btc_cfg = cfg.bitcoin
    if btc_cfg is None or cfg.simpleproof is None:
        raise RuntimeError("config missing required sections")
    min_confs = btc_cfg.confirmations
    btc = _get_btc_rpc(cfg)
    last_height: int | None = None
    last_tip_hash: str | None = None
    stream = _get_zmq_subscriber(cfg)
    stream_cm = cast(
        "contextlib.AbstractContextManager[HashBlockStream]",
        stream if hasattr(stream, "__enter__") else contextlib.nullcontext(stream),
    )
    try:
        with stream_cm as zmq_sub:
            while True:
                block = zmq_sub.wait_for_block(timeout_ms=5000)
                if block is None:
                    continue
                try:
                    with with_lock(staging_dir, timeout=_LOCK_TIMEOUT_SECONDS):
                        info = btc.getblockchaininfo()
                        height_jumped = (
                            last_height is not None and info.height - last_height > 1
                        )
                        tip_changed_at_stable = (
                            last_height is not None
                            and info.height == last_height
                            and last_tip_hash is not None
                            and last_tip_hash != info.best_block_hash
                        )
                        if height_jumped or tip_changed_at_stable:
                            sp = _get_sp_client(cfg)
                            state = load_ots(staging_dir)
                            state = _inventory_reverify_existing_proof(
                                state, btc, sp, staging_dir, min_confs
                            )
                            save_ots(staging_dir, state)
                        last_height = info.height
                        last_tip_hash = info.best_block_hash
                        _inventory_upgrade_pass_locked(cfg, staging_dir)
                        state = load_ots(staging_dir)
                        if state.phase is Phase.INVENTORY_UPGRADED:
                            return 0
                except LockTimeoutError as e:
                    logger.warning(
                        "staging lock contended (%s); retrying on next block", e
                    )
                    continue
    except KeyboardInterrupt:
        return 130


@timestamp_app.command("inventory")
def timestamp_inventory(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    upgrade: Annotated[
        bool,
        typer.Option(
            "--upgrade",
            help="Poll SP, verify inventory .ots against Bitcoin, advance phase.",
        ),
    ] = False,
    watch: Annotated[
        bool,
        typer.Option(
            "--watch",
            help="ZMQ-driven loop; runs an inventory upgrade pass per block.",
        ),
    ] = False,
    retry: Annotated[
        bool,
        typer.Option("--retry", help="Re-POST inventory digest at inventory-pending."),
    ] = False,
) -> None:
    """Build inventory, submit its digest to SimpleProof, serialize plan."""
    cfg: Config | None = ctx.obj.get("config")
    if cfg is None or cfg.simpleproof is None:
        err("'stage timestamp inventory' requires [simpleproof] config section")
        raise typer.Exit(2)
    if retry:
        for other_name, other_set in (("--upgrade", upgrade), ("--watch", watch)):
            if other_set:
                err(
                    "'stage timestamp inventory --retry' is incompatible with "
                    f"{other_name}"
                )
                raise typer.Exit(2)
    if upgrade and cfg.bitcoin is None:
        err("'stage timestamp inventory --upgrade' requires [bitcoin] config section")
        raise typer.Exit(2)
    if watch and not upgrade:
        err("'stage timestamp inventory --watch' requires --upgrade")
        raise typer.Exit(2)

    store: StagingStore = ctx.obj["staging"]
    sr, _fs = open_root(ctx.obj.get("root"))
    root_path = _resolve_root_path(ctx)

    if not store.exists(root_path, object_id):
        err(f"no staging exists for {object_id!r}")
        raise typer.Exit(1)

    staging_dir = store.staging_path(root_path, object_id)
    if not has_ots(staging_dir):
        err(f"{object_id!r} is not an OTS staging (run 'stage new --ots' first)")
        raise typer.Exit(2)

    if watch:
        try:
            exit_code = _inventory_watch_loop(cfg, staging_dir)
        except (ReorgDetectedError, AttestationMismatchError) as e:
            err(f"halt: {e}")
            raise typer.Exit(5) from None
        except PhaseError as e:
            err(str(e))
            raise typer.Exit(3) from None
        raise typer.Exit(exit_code)

    if upgrade:
        try:
            _inventory_upgrade_pass(cfg, staging_dir)
        except (ReorgDetectedError, AttestationMismatchError) as e:
            err(f"halt: {e}")
            raise typer.Exit(5) from None
        except PhaseError as e:
            err(str(e))
            raise typer.Exit(3) from None
        return

    if retry:
        with with_lock(staging_dir, timeout=_LOCK_TIMEOUT_SECONDS):
            state = load_ots(staging_dir)
            try:
                check_transition(state.phase, "timestamp inventory --retry")
            except PhaseError as e:
                err(str(e))
                raise typer.Exit(3) from None
            category_id = apply_category_id(state, cfg.simpleproof.proof_category_id)
            if state.inventory_proof is None:
                err("inventory_proof missing")
                raise typer.Exit(3)
            inv_digest = state.inventory_proof.digest
            sp = _get_sp_client(cfg)
            sp.submit_hashes([inv_digest], category_id)
            logger.info("retried inventory submission for %s", object_id)
        return

    with with_lock(staging_dir, timeout=_LOCK_TIMEOUT_SECONDS):
        state = load_ots(staging_dir)
        try:
            target_phase = check_transition(state.phase, "timestamp inventory")
        except PhaseError as e:
            err(str(e) + "; run 'stage timestamp content' first")
            raise typer.Exit(3) from None
        category_id = apply_category_id(state, cfg.simpleproof.proof_category_id)

        if state.phase is Phase.INVENTORY_UPGRADED:
            return

        # On resubmit, preserve `created` from the prior plan so the new
        # inventory digest stays identical across retries.
        prior_created: str | None = None
        if state.inventory_proof is not None and plan_path(staging_dir).is_file():
            prior_plan = deserialize(plan_path(staging_dir).read_bytes())
            prior_inv = prior_plan.new_inv.inventory
            prior_created = prior_inv.versions[prior_inv.head].created

        plan, _cs = _build_inventory_plan(
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

        sp = _get_sp_client(cfg)
        sp.submit_hashes([inv_digest], category_id)

        plan_path(staging_dir).write_bytes(serialize(plan))

        _log_phase(prev_phase, target_phase)
        logger.info("submitted inventory digest %s for %s", inv_digest, object_id)


@stage_app.command()
def reset(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Discard persisted OTS progress (submissions, proofs, etc).",
        ),
    ] = False,
) -> None:
    """Delete staging area for an object."""
    store: StagingStore = ctx.obj["staging"]
    root_path = _resolve_root_path(ctx)

    if not store.exists(root_path, object_id):
        typer.echo(f"No staging exists for {object_id!r}", err=True)
        return

    staging_dir = store.staging_path(root_path, object_id)
    if has_ots(staging_dir):
        state = load_ots(staging_dir)
        counts = (
            ("submissions", len(state.submissions)),
            ("verify_jobs", len(state.verify_jobs)),
            ("content_proofs", len(state.content_proofs)),
            ("inventory_proof", 1 if state.inventory_proof is not None else 0),
        )
        if any(n for _, n in counts):
            phase = state.phase.value
            summary = ", ".join(f"{k}={v}" for k, v in counts)
            if not force:
                err(
                    f"reset would discard persisted OTS progress at phase "
                    f"{phase!r} ({summary}); re-run with --force to proceed"
                )
                raise typer.Exit(1)
            logger.warning(
                "reset --force discarding OTS progress at phase %r: %s",
                phase,
                summary,
            )

    store.purge(root_path, object_id)
