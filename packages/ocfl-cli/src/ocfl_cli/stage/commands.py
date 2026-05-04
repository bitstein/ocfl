"""Typer callbacks for the stage command group.

Thin glue: parse options, resolve context, call into ``service`` /
``watch``, render results. Business logic lives in those modules.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from platformdirs import user_state_path
from rich.text import Text

from ocfl_cli import _appctx as appctx
from ocfl_cli import _style
from ocfl_cli._common import err, open_root, output_json
from ocfl_cli._exit import exit_for_error
from ocfl_cli.errors import (
    AttestationMismatchError,
    ConfigError,
    PhaseError,
    ReorgDetectedError,
    StagingError,
)
from ocfl_cli.stage import service, watch
from ocfl_cli.stage.fsm import Phase
from ocfl_cli.stage.ingest import ingest_file
from ocfl_cli.stage.state import has_ots, load_ots, save_ots, with_lock
from ocfl_cli.stage.store import StagingStore

if TYPE_CHECKING:
    from ocfl_cli._appctx import BtcRpcFactory, SpClientFactory
    from ocfl_cli.config import Config, SimpleProofConfig


logger = logging.getLogger(__name__)


stage_app = typer.Typer(
    name="stage",
    help="Stage changes for OCFL objects.",
    no_args_is_help=True,
)


# ---------------------------------------------------------------------------
# Helpers (typer-aware).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StagingResolved:
    store: StagingStore
    root_path: str
    staging_dir: Path


@dataclass(frozen=True, slots=True)
class TimestampPreflight:
    cfg: Config
    resolved: StagingResolved
    sp_factory: SpClientFactory
    btc_factory: BtcRpcFactory


def _resolve_root_path(ctx: typer.Context) -> str:
    """Resolve absolute root path string for staging path hashing."""
    return str(Path(appctx.root(ctx) or Path.cwd()).resolve())


def _stage_store(ctx: typer.Context) -> StagingStore:
    obj = ctx.obj
    if not isinstance(obj, dict):
        raise RuntimeError("internal error: expected ctx.obj to be a dict")
    store = obj.get("staging")
    if not isinstance(store, StagingStore):
        raise RuntimeError("internal error: missing ctx.obj['staging']")
    return store


def require_config(ctx: typer.Context) -> Config:
    return appctx.config(ctx)


def require_simpleproof(ctx: typer.Context, cfg: Config) -> None:
    if cfg.simpleproof is None:
        exit_for_error(
            ConfigError(
                f"{appctx.command_path(ctx)!r} requires [simpleproof] config section"
            )
        )


def require_bitcoin(ctx: typer.Context, cfg: Config) -> None:
    if cfg.bitcoin is None:
        exit_for_error(
            ConfigError(
                f"{appctx.command_path(ctx)!r} requires [bitcoin] config section"
            )
        )


def validate_timestamp_flags(
    ctx: typer.Context, *, upgrade: bool, watch: bool, retry: bool
) -> None:
    command = appctx.command_path(ctx)
    # These are post-parse usage errors: Click accepted the tokens, but the
    # resulting invocation shape is invalid.
    if retry:
        for other_name, other_set in (("--upgrade", upgrade), ("--watch", watch)):
            if other_set:
                err(f"'{command} --retry' is incompatible with {other_name}")
                raise typer.Exit(2)
    if watch and not upgrade:
        err(f"'{command} --watch' requires --upgrade")
        raise typer.Exit(2)


def require_existing_staging(
    ctx: typer.Context, object_id: str, *, hint: str | None = None
) -> StagingResolved:
    store = _stage_store(ctx)
    root_path = _resolve_root_path(ctx)
    staging_dir = store.staging_path(root_path, object_id)
    if not store.exists(root_path, object_id):
        message = f"no staging exists for {object_id!r}"
        if hint is not None:
            message = f"{message} ({hint})"
        exit_for_error(StagingError(message))
    return StagingResolved(store=store, root_path=root_path, staging_dir=staging_dir)


def require_ots_staging(staging_dir: Path, object_id: str) -> None:
    if not has_ots(staging_dir):
        exit_for_error(
            StagingError(
                f"{object_id!r} is not an OTS staging (run 'stage new --ots' first)"
            )
        )


def get_factories(ctx: typer.Context) -> tuple[SpClientFactory, BtcRpcFactory]:
    return appctx.sp_client_factory(ctx), appctx.btc_rpc_factory(ctx)


def require_timestamp_preflight(
    ctx: typer.Context,
    object_id: str,
    *,
    upgrade: bool,
    watch: bool,
    retry: bool,
) -> TimestampPreflight:
    cfg = require_config(ctx)
    require_simpleproof(ctx, cfg)
    validate_timestamp_flags(ctx, upgrade=upgrade, watch=watch, retry=retry)
    if upgrade:
        require_bitcoin(ctx, cfg)

    resolved = require_existing_staging(ctx, object_id)
    require_ots_staging(resolved.staging_dir, object_id)
    sp_factory, btc_factory = get_factories(ctx)
    return TimestampPreflight(
        cfg=cfg,
        resolved=resolved,
        sp_factory=sp_factory,
        btc_factory=btc_factory,
    )


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
        exit_for_error(ConfigError(f"--ots requires [simpleproof] section {where}"))
    if cfg.bitcoin is None:
        exit_for_error(ConfigError(f"--ots requires [bitcoin] section {where}"))
    return cfg.simpleproof


# ---------------------------------------------------------------------------
# stage callback + lifecycle commands.
# ---------------------------------------------------------------------------


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

    cfg = appctx.config(ctx)
    sp_cfg: SimpleProofConfig | None = None
    if ots:
        sp_cfg = _require_ots_config(cfg, digest)
        digest = "sha256"

    store = _stage_store(ctx)
    sr, _fs = open_root(appctx.root(ctx))
    root_path = _resolve_root_path(ctx)

    if store.exists(root_path, object_id):
        err(f"staging already exists for {object_id!r}")
        raise typer.Exit(1)

    obj = sr.get_object(object_id)
    if obj.exists:
        head_inv = obj.inventory.inventory
        builder = InventoryBuilder(head_inv).add_version(
            {}, created="1970-01-01T00:00:00Z"
        )
        inv = builder.build()
    else:
        alg = DigestAlgorithm.SHA512
        digest_name = digest
        if digest_name is None:
            digest_name = cfg.effective_digest(appctx.archive(ctx))
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
    from ocfl_lib import DigestAlgorithm

    resolved = require_existing_staging(ctx, object_id, hint="run 'stage new' first")
    store = resolved.store
    root_path = resolved.root_path
    staging_dir = resolved.staging_dir

    inv = store.load_inventory(root_path, object_id)
    alg = DigestAlgorithm.from_ocfl_name(inv.digest_algorithm)
    head = inv.head
    state = dict(inv.versions[head].state)
    content_map = store.load_content_map(root_path, object_id)

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

    store.save_inventory(root_path, object_id, service._update_head_state(inv, state))
    store.save_content_map(root_path, object_id, content_map)


@stage_app.command()
def mv(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    src_path: Annotated[str, typer.Argument(help="Source logical path.")],
    dst_path: Annotated[str, typer.Argument(help="Destination logical path.")],
) -> None:
    """Rename a logical path in the staged version."""
    resolved = require_existing_staging(ctx, object_id)
    store = resolved.store
    root_path = resolved.root_path
    staging_dir = resolved.staging_dir

    inv = store.load_inventory(root_path, object_id)
    state = {d: list(ps) for d, ps in inv.versions[inv.head].state.items()}

    found_digest = None
    for d, paths in state.items():
        if src_path in paths:
            found_digest = d
            break

    if found_digest is None:
        err(f"path not found: {src_path!r}")
        raise typer.Exit(1)

    state[found_digest].remove(src_path)
    state[found_digest].append(dst_path)

    if has_ots(staging_dir):
        with with_lock(staging_dir, timeout=service._LOCK_TIMEOUT_SECONDS):
            service._pair_ots_companion_rename(state, src_path, dst_path)
            store.save_inventory(
                root_path, object_id, service._update_head_state(inv, state)
            )
            service._apply_rm_mv_regression(
                staging_dir, state, "mv", f"stage mv {src_path} {dst_path}"
            )
    else:
        store.save_inventory(
            root_path, object_id, service._update_head_state(inv, state)
        )


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
    resolved = require_existing_staging(ctx, object_id)
    store = resolved.store
    root_path = resolved.root_path
    staging_dir = resolved.staging_dir

    inv = store.load_inventory(root_path, object_id)
    state = {d: list(ps) for d, ps in inv.versions[inv.head].state.items()}
    content_map = store.load_content_map(root_path, object_id)

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
        store.save_inventory(
            root_path, object_id, service._update_head_state(inv, state)
        )
        store.save_content_map(root_path, object_id, content_map)

    if has_ots(staging_dir):
        with with_lock(staging_dir, timeout=service._LOCK_TIMEOUT_SECONDS):
            service._pair_ots_companion_remove(state, removed_paths)
            _finalize()
            service._apply_rm_mv_regression(
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
    resolved = require_existing_staging(ctx, object_id)
    store = resolved.store
    root_path = resolved.root_path

    inv = store.load_inventory(root_path, object_id)
    state = inv.versions[inv.head].state

    entries: list[tuple[str, str]] = []
    for d, paths in state.items():
        for p in paths:
            entries.append((p, d))
    entries.sort()

    if appctx.json_mode(ctx):
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


# ---------------------------------------------------------------------------
# status (renders service-built dicts).
# ---------------------------------------------------------------------------


def _render_status_diff(data: dict) -> None:
    action_prefix = {"added": "A", "deleted": "D", "modified": "M", "renamed": "R"}
    sym_style = {
        "A": _style.added,
        "D": _style.deleted,
        "M": _style.modified,
        "R": _style.renamed,
    }
    for c in data["changes"]:
        prefix = action_prefix.get(c["action"], "?")
        styler = sym_style.get(prefix)
        styled_sym = styler(prefix) if styler else Text(prefix)
        parts: list = [styled_sym, " ", c["path"]]
        if c.get("renamed_from"):
            parts.append(f" (from {c['renamed_from']})")
        _style.echo(Text.assemble(*parts))


def _render_status_ots(data: dict) -> None:
    counts = data["counts"]
    parts = [
        f"Phase: {data['phase']} ({counts['upgraded']} of {counts['total']} upgraded"
    ]
    if counts["pending"]:
        parts.append(f", {counts['pending']} outstanding")
    if counts["failed"]:
        parts.append(f", {counts['failed']} failed")
    parts.append(")")
    typer.echo("".join(parts))

    outstanding = data["outstanding_jobs"]
    if outstanding:
        typer.echo("Outstanding:")
        for o in outstanding:
            short = f"{o['digests'][0][:12]}..." if o["digests"] else ""
            typer.echo(
                f"  sha256:{short} — job {o['job_id']}, {o['status']}, "
                f"submitted {service.humanize_age(o['age_seconds'])}"
            )

    bitcoin = data["bitcoin"]
    if bitcoin is not None:
        sync_str = "node in sync" if bitcoin["in_sync"] else "node not in sync"
        typer.echo(f"Bitcoin: height {bitcoin['height']} ({sync_str})")

    typer.echo(
        "SimpleProof: configured"
        if data["simpleproof"]["configured"]
        else "SimpleProof: not configured"
    )
    typer.echo(f"Next: {data['next_action']}")


@stage_app.command()
def status(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
) -> None:
    """Show changes in staged version vs committed head (OTS-aware)."""
    resolved = require_existing_staging(ctx, object_id)
    store = resolved.store
    root_path = resolved.root_path
    staging_dir = resolved.staging_dir

    json_mode = appctx.json_mode(ctx)
    if has_ots(staging_dir):
        cfg = appctx.config(ctx)
        data = service.status_ots_dict(
            cfg,
            store,
            root_path,
            object_id,
            staging_dir,
            btc_factory=appctx.btc_rpc_factory(ctx),
        )
        if json_mode:
            output_json(data)
        else:
            _render_status_ots(data)
    else:
        data = service.status_diff_dict(store, root_path, object_id)
        if json_mode:
            output_json(data)
        else:
            _render_status_diff(data)


# ---------------------------------------------------------------------------
# commit.
# ---------------------------------------------------------------------------


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
    from ocfl_cli.stage.state import plan_path
    from ocfl_lib.inventory import UserInfo

    sr, fs = open_root(appctx.root(ctx))
    resolved = require_existing_staging(ctx, object_id)
    store = resolved.store
    root_path = resolved.root_path
    staging_dir = resolved.staging_dir

    obj = sr.get_object(object_id)

    # OTS commit path: phase=INVENTORY_UPGRADED + serialized plan on disk.
    # Both conditions required (a partial state means the OTS workflow has
    # not reached the deferred-completion point yet).
    if has_ots(staging_dir) and plan_path(staging_dir).is_file():
        try:
            service.commit_ots(
                store=store,
                fs=fs,
                obj_dir=obj._path,
                root_path=root_path,
                object_id=object_id,
                staging_dir=staging_dir,
            )
        except PhaseError as e:
            err(
                f"{e}; run 'stage timestamp inventory --upgrade' to advance "
                f"to phase=inventory-upgraded"
            )
            raise typer.Exit(3) from None
        except AttestationMismatchError as e:
            err(str(e))
            raise typer.Exit(5) from None
        except service.CommitFailedError as e:
            err(str(e))
            raise typer.Exit(130 if e.interrupted else 1) from None
        return

    user = None
    if name:
        user = UserInfo(name=name, address=address)

    try:
        service.commit_bare(
            store=store,
            sr=sr,
            fs=fs,
            obj_dir=obj._path,
            root_path=root_path,
            object_id=object_id,
            message=message,
            user=user,
            created=created,
        )
    except (service.StagingFileMissingError, service.StagingFileChangedError) as e:
        err(str(e))
        raise typer.Exit(1) from None
    except service.CommitFailedError as e:
        err(str(e))
        raise typer.Exit(130 if e.interrupted else 1) from None


# ---------------------------------------------------------------------------
# timestamp content / inventory.
# ---------------------------------------------------------------------------


timestamp_app = typer.Typer(
    name="timestamp",
    help="Timestamp staged content / inventory via SimpleProof.",
    no_args_is_help=True,
)
stage_app.add_typer(timestamp_app)


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
    watch_flag: Annotated[
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
    preflight = require_timestamp_preflight(
        ctx,
        object_id,
        upgrade=upgrade,
        watch=watch_flag,
        retry=retry,
    )
    cfg = preflight.cfg
    resolved = preflight.resolved

    sr, _fs = open_root(appctx.root(ctx))

    if watch_flag:
        try:
            exit_code = watch._watch_loop(
                cfg,
                resolved.store,
                sr,
                resolved.root_path,
                object_id,
                resolved.staging_dir,
                sp_factory=preflight.sp_factory,
                btc_factory=preflight.btc_factory,
            )
        except (ReorgDetectedError, AttestationMismatchError) as e:
            err(f"halt: {e}")
            raise typer.Exit(5) from None
        raise typer.Exit(exit_code)

    if upgrade:
        try:
            service._upgrade_pass(
                cfg,
                resolved.store,
                sr,
                resolved.root_path,
                object_id,
                resolved.staging_dir,
                sp_factory=preflight.sp_factory,
                btc_factory=preflight.btc_factory,
            )
        except (ReorgDetectedError, AttestationMismatchError) as e:
            err(f"halt: {e}")
            raise typer.Exit(5) from None
        return

    if retry:
        with with_lock(resolved.staging_dir, timeout=service._LOCK_TIMEOUT_SECONDS):
            try:
                service.retry_content_locked(
                    cfg,
                    resolved.staging_dir,
                    object_id,
                    sp_factory=preflight.sp_factory,
                )
            except PhaseError as e:
                err(str(e))
                raise typer.Exit(3) from None
            except service.NoFailedSubmissionsError as e:
                typer.echo(str(e))
                return
        return

    with with_lock(resolved.staging_dir, timeout=service._LOCK_TIMEOUT_SECONDS):
        try:
            advanced = service.submit_content_locked(
                cfg,
                resolved.store,
                sr,
                resolved.root_path,
                object_id,
                resolved.staging_dir,
                sp_factory=preflight.sp_factory,
            )
        except PhaseError as e:
            err(str(e))
            raise typer.Exit(3) from None
        if advanced:
            typer.echo("nothing to timestamp; advancing to content-upgraded")


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
    watch_flag: Annotated[
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
    preflight = require_timestamp_preflight(
        ctx,
        object_id,
        upgrade=upgrade,
        watch=watch_flag,
        retry=retry,
    )
    cfg = preflight.cfg
    resolved = preflight.resolved

    sr, _fs = open_root(appctx.root(ctx))

    if watch_flag:
        try:
            exit_code = watch._inventory_watch_loop(
                cfg,
                resolved.staging_dir,
                sp_factory=preflight.sp_factory,
                btc_factory=preflight.btc_factory,
            )
        except (ReorgDetectedError, AttestationMismatchError) as e:
            err(f"halt: {e}")
            raise typer.Exit(5) from None
        except PhaseError as e:
            err(str(e))
            raise typer.Exit(3) from None
        raise typer.Exit(exit_code)

    if upgrade:
        try:
            service._inventory_upgrade_pass(
                cfg,
                resolved.staging_dir,
                sp_factory=preflight.sp_factory,
                btc_factory=preflight.btc_factory,
            )
        except (ReorgDetectedError, AttestationMismatchError) as e:
            err(f"halt: {e}")
            raise typer.Exit(5) from None
        except PhaseError as e:
            err(str(e))
            raise typer.Exit(3) from None
        return

    if retry:
        with with_lock(resolved.staging_dir, timeout=service._LOCK_TIMEOUT_SECONDS):
            try:
                service.retry_inventory_locked(
                    cfg,
                    resolved.staging_dir,
                    object_id,
                    sp_factory=preflight.sp_factory,
                )
            except PhaseError as e:
                err(str(e))
                raise typer.Exit(3) from None
            except service.InventoryProofMissingError as e:
                err(str(e))
                raise typer.Exit(3) from None
        return

    with with_lock(resolved.staging_dir, timeout=service._LOCK_TIMEOUT_SECONDS):
        try:
            service.submit_inventory_locked(
                cfg,
                resolved.store,
                sr,
                resolved.root_path,
                object_id,
                resolved.staging_dir,
                sp_factory=preflight.sp_factory,
            )
        except PhaseError as e:
            err(str(e) + "; run 'stage timestamp content' first")
            raise typer.Exit(3) from None


# ---------------------------------------------------------------------------
# reset.
# ---------------------------------------------------------------------------


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
    store = _stage_store(ctx)
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


__all__ = ["stage_app"]
