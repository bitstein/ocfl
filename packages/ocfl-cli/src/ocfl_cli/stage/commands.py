"""Typer callbacks for the stage command group.

Thin glue: parse options, resolve context, call into ``service`` /
``watch``, render results. Business logic lives in those modules.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import msgspec
import typer
from platformdirs import user_state_path
from rich.text import Text

from ocfl_cli import _appctx as appctx
from ocfl_cli import _style
from ocfl_cli._common import open_root, output_json
from ocfl_cli._exit import exit_for_error
from ocfl_cli._io import err
from ocfl_cli.archive.config import validate_digest_name
from ocfl_cli.errors import (
    AttestationMismatchError,
    ConfigError,
    LockTimeoutError,
    MissingStagedMetadataError,
    PhaseError,
    ReorgDetectedError,
    SimpleProofError,
    StagingError,
)
from ocfl_cli.stage import batch as batch_state
from ocfl_cli.stage import service, watch
from ocfl_cli.stage.fsm import Phase
from ocfl_cli.stage.ingest import ingest_file
from ocfl_cli.stage.schemas import StagedMetadata
from ocfl_cli.stage.state import (
    OtsState,
    VerifiedProof,
    has_ots,
    load_ots,
    save_ots,
    with_lock,
)
from ocfl_cli.stage.store import StagingStore
from ocfl_lib import DigestAlgorithm

if TYPE_CHECKING:
    from ocfl_cli._appctx import BtcRpcFactory, SpClientFactory
    from ocfl_cli.config import Config, SimpleProofConfig


logger = logging.getLogger(__name__)

OTS_HELP_PANEL = "OTS"


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


class DigestOption(StrEnum):
    SHA512 = "sha512"
    SHA256 = "sha256"
    SHA1 = "sha1"
    MD5 = "md5"
    BLAKE2B_512 = "blake2b-512"
    BLAKE2B_256 = "blake2b-256"
    BLAKE2B_160 = "blake2b-160"
    SHA512_256 = "sha512/256"


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


def validate_inventory_metadata_flag(
    ctx: typer.Context,
    *,
    upgrade: bool,
    watch: bool,
    retry: bool,
    allow_missing_metadata: bool,
) -> None:
    """Reject metadata-bypass flag in inventory modes where it has no effect."""
    if not allow_missing_metadata:
        return
    if upgrade or watch or retry:
        command = appctx.command_path(ctx)
        err(
            f"'{command} --allow-missing-metadata' is only valid when submitting "
            "an inventory timestamp"
        )
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


def _require_ots_config(
    cfg: Config | None, digest: DigestOption | None
) -> SimpleProofConfig:
    """Preflight `stage new --ots`. Returns validated [simpleproof] config."""
    if digest is not None and digest is not DigestOption.SHA256:
        err(
            f"--ots requires SHA-256 (SimpleProof API constraint); "
            f"got --digest={digest.value!r}"
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


def _require_spec_digest(digest_name: str) -> DigestAlgorithm:
    try:
        validate_digest_name(digest_name)
    except ValueError:
        err("content digest must be sha512 or sha256")
        raise typer.Exit(2) from None
    return DigestAlgorithm.from_ocfl_name(digest_name)


def _validate_logical_path(logical_path: str) -> None:
    if (
        not logical_path
        or logical_path.startswith("/")
        or logical_path.endswith("/")
        or any(part in ("", ".", "..") for part in logical_path.split("/"))
    ):
        err(f"invalid logical path: {logical_path!r}")
        raise typer.Exit(1)


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
    message: Annotated[
        str | None,
        typer.Option("-m", "--message", help="Version message metadata."),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option("--name", help="Version user name metadata."),
    ] = None,
    address: Annotated[
        str | None,
        typer.Option("--address", help="Version user address metadata."),
    ] = None,
    digest: Annotated[
        DigestOption | None,
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
    from ocfl_lib.inventory_builder import InventoryBuilder

    try:
        metadata = service.staged_metadata_from_options(
            message=message,
            name=name,
            address=address,
        )
    except service.MetadataInputError as e:
        raise typer.BadParameter(str(e)) from None
    cfg = appctx.config(ctx)
    sp_cfg: SimpleProofConfig | None = None
    if ots:
        sp_cfg = _require_ots_config(cfg, digest)
        digest = DigestOption.SHA256

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
        digest_name = digest.value if digest is not None else None
        if digest_name is None:
            digest_name = cfg.effective_digest(appctx.archive(ctx))
        if digest_name:
            alg = _require_spec_digest(digest_name)

        inv = (
            InventoryBuilder()
            .id(object_id)
            .spec(sr.spec)
            .digest_algorithm(alg)
            .add_version({}, created="1970-01-01T00:00:00Z")
            .build()
        )

    store.create(root_path, object_id, inv)
    if metadata != StagedMetadata():
        store.save_metadata(root_path, object_id, metadata)

    if sp_cfg is not None:
        save_ots(
            store.staging_path(root_path, object_id),
            OtsState(phase=Phase.DRAFTING, category_id=sp_cfg.proof_category_id),
        )


@stage_app.command()
def metadata(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    message: Annotated[
        str | None,
        typer.Option("-m", "--message", help="Set version message metadata."),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option("--name", help="Set version user name metadata."),
    ] = None,
    address: Annotated[
        str | None,
        typer.Option("--address", help="Set version user address metadata."),
    ] = None,
    clear_message: Annotated[
        bool,
        typer.Option("--clear-message", help="Clear version message metadata."),
    ] = False,
    clear_name: Annotated[
        bool,
        typer.Option("--clear-name", help="Clear version user name metadata."),
    ] = False,
    clear_address: Annotated[
        bool,
        typer.Option("--clear-address", help="Clear version user address metadata."),
    ] = False,
) -> None:
    """Edit staged version metadata."""
    resolved = require_existing_staging(ctx, object_id, hint="run 'stage new' first")
    try:
        service.validate_metadata_update_options(
            message=message,
            name=name,
            address=address,
            clear_message=clear_message,
            clear_name=clear_name,
            clear_address=clear_address,
        )
        with with_lock(resolved.staging_dir, timeout=service._LOCK_TIMEOUT_SECONDS):

            def invalidate_ots_if_needed(
                current: StagedMetadata, updated: StagedMetadata
            ) -> None:
                if has_ots(resolved.staging_dir) and updated != current:
                    service.apply_metadata_ots_regression(resolved.staging_dir)

            service.update_metadata(
                resolved.store,
                resolved.root_path,
                object_id,
                message=message,
                name=name,
                address=address,
                clear_message=clear_message,
                clear_name=clear_name,
                clear_address=clear_address,
                before_save=invalidate_ots_if_needed,
            )
    except service.MetadataInputError as e:
        raise typer.BadParameter(str(e)) from None
    except LockTimeoutError as e:
        exit_for_error(e)


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
        _validate_logical_path(logical_path)
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
    _validate_logical_path(dst_path)
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


def _render_status_section(title: str, *, leading_blank: bool = False) -> None:
    if leading_blank:
        typer.echo()
    _style.echo(_style.key(f"{title}:"))


def _render_metadata_status(
    data: dict[str, str | None], *, leading_blank: bool = False
) -> None:
    _render_status_section("Metadata", leading_blank=leading_blank)
    for field in ("message", "user.name", "user.address"):
        value = data[field] if data[field] is not None else "missing"
        typer.echo(f"  {field}: {value}")


def _plural(n: int, singular: str, plural: str | None = None) -> str:
    return singular if n == 1 else (plural or f"{singular}s")


def _render_watch_start(object_id: str) -> None:
    _render_status_section("Watch")
    typer.echo(f"  Object: {object_id}")
    typer.echo("  Waiting for Bitcoin blocks")


def _render_watch_progress(kind: str, data: dict, *, next_action: str) -> None:
    if kind == "block":
        _render_status_section("Block", leading_blank=True)
        typer.echo(f"  Height: {data['height']}")
        typer.echo("  Checking SimpleProof jobs")
        typer.echo(f"  Phase: {data['phase']}")
        if data.get("reverify"):
            typer.echo("  Rechecking cached proofs after chain movement")
        return
    if kind == "advanced":
        _render_status_section("Workflow", leading_blank=True)
        typer.echo(f"  Phase: {data['previous_phase']} -> {data['phase']}")
        typer.echo(f"  Next: {next_action}")


def _render_content_upgrade_result(
    object_id: str, result: service.UpgradeContentResult
) -> None:
    _render_status_section("Content")
    typer.echo(f"  Object: {object_id}")
    typer.echo("  Checked SimpleProof jobs")
    if result.unverified_pending_count:
        count = result.unverified_pending_count
        typer.echo(f"  Waiting on {count} pending {_plural(count, 'digest')}")
    if result.orphan_count:
        count = result.orphan_count
        typer.echo(f"  Held by {count} orphan {_plural(count, 'digest')}")

    _render_status_section("Workflow", leading_blank=True)
    if result.previous_phase is result.phase:
        typer.echo(f"  Phase: {result.phase.value}")
    else:
        typer.echo(f"  Phase: {result.previous_phase.value} -> {result.phase.value}")
    next_action = (
        "stage timestamp inventory"
        if result.phase is Phase.CONTENT_UPGRADED
        else "stage timestamp content --upgrade --watch"
    )
    typer.echo(f"  Next: {next_action}")


def _render_status_ots(data: dict) -> None:
    _render_status_section("Object")
    if data["changes"]:
        _render_status_diff({"changes": data["changes"]})
    else:
        typer.echo("  no staged changes")

    _render_metadata_status(data["metadata"], leading_blank=True)

    _render_status_section("Workflow", leading_blank=True)
    counts = data["counts"]
    parts = [
        f"  Phase: {data['phase']} ({counts['upgraded']} of {counts['total']} upgraded"
    ]
    if counts["pending"]:
        parts.append(f", {counts['pending']} outstanding")
    if counts["failed"]:
        parts.append(f", {counts['failed']} failed")
    parts.append(")")
    typer.echo("".join(parts))

    outstanding = data["outstanding_jobs"]
    if outstanding:
        typer.echo("  Outstanding:")
        for o in outstanding:
            short = f"{o['digests'][0][:12]}..." if o["digests"] else ""
            typer.echo(
                f"    sha256:{short} — job {o['job_id']}, {o['status']}, "
                f"submitted {service.humanize_age(o['age_seconds'])}"
            )
    typer.echo(f"  Next: {data['next_action']}")

    _render_status_section("Network", leading_blank=True)
    bitcoin = data["bitcoin"]
    if bitcoin is not None:
        sync_str = "node in sync" if bitcoin["in_sync"] else "node not in sync"
        typer.echo(f"  Bitcoin: height {bitcoin['height']} ({sync_str})")
    else:
        typer.echo("  Bitcoin: unavailable")

    typer.echo(
        "  SimpleProof: configured"
        if data["simpleproof"]["configured"]
        else "  SimpleProof: not configured"
    )


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
            _render_metadata_status(
                data["metadata"], leading_blank=bool(data["changes"])
            )


# ---------------------------------------------------------------------------
# commit.
# ---------------------------------------------------------------------------


@stage_app.command()
def commit(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    allow_missing_metadata: Annotated[
        bool,
        typer.Option(
            "--allow-missing-metadata",
            help="Commit even when staged version metadata is incomplete.",
        ),
    ] = False,
) -> None:
    """Commit staged changes to the storage root."""
    sr, fs = open_root(appctx.root(ctx))
    resolved = require_existing_staging(ctx, object_id)
    store = resolved.store
    root_path = resolved.root_path
    staging_dir = resolved.staging_dir

    obj = sr.get_object(object_id)

    # OTS commit path: ots.json makes this an OTS staging even when the
    # deferred inventory plan is missing or stale.
    if has_ots(staging_dir):
        try:
            service.commit_ots(
                store=store,
                fs=fs,
                obj_dir=obj._path,
                root_path=root_path,
                object_id=object_id,
                staging_dir=staging_dir,
                allow_missing_metadata=allow_missing_metadata,
            )
        except PhaseError as e:
            exit_for_error(
                e,
                message=(
                    f"{e}; run 'stage timestamp inventory' and then "
                    f"'stage timestamp inventory --upgrade' to refresh the "
                    f"timestamped inventory plan"
                ),
            )
        except AttestationMismatchError as e:
            exit_for_error(e)
        except MissingStagedMetadataError as e:
            exit_for_error(e)
        except service.CommitFailedError as e:
            exit_for_error(e)
        return

    try:
        service.commit_bare(
            store=store,
            sr=sr,
            fs=fs,
            obj_dir=obj._path,
            root_path=root_path,
            object_id=object_id,
            staging_dir=staging_dir,
            allow_missing_metadata=allow_missing_metadata,
        )
    except (
        service.StagingFileMissingError,
        service.StagingFileChangedError,
        LockTimeoutError,
    ) as e:
        exit_for_error(e)
    except MissingStagedMetadataError as e:
        exit_for_error(e)
    except service.CommitFailedError as e:
        exit_for_error(e)


# ---------------------------------------------------------------------------
# timestamp content / inventory.
# ---------------------------------------------------------------------------


timestamp_app = typer.Typer(
    name="timestamp",
    help="Timestamp staged content / inventory via SimpleProof.",
    no_args_is_help=True,
)
stage_app.add_typer(timestamp_app, rich_help_panel=OTS_HELP_PANEL)

batch_app = typer.Typer(
    name="batch",
    help="Inspect and clean up batch timestamping state.",
    no_args_is_help=True,
)
timestamp_app.add_typer(batch_app)


_BATCH_STATUSES = ("prepared", "submitted", "upgraded", "failed", "skipped")
_BATCH_MISSING_NEXT = "Operate per-object via stage timestamp content/inventory"


def _batch_counts(batch: batch_state.BatchFile) -> dict[str, int]:
    counts = dict.fromkeys(_BATCH_STATUSES, 0)
    for row in batch.objects:
        counts[row.status] += 1
    counts["total"] = len(batch.objects)
    return counts


def _batch_to_list_row(batch: batch_state.BatchFile) -> dict:
    counts = _batch_counts(batch)
    return {
        "batch_id": batch.batch_id,
        "short_batch_id": batch.batch_id.split("-", 1)[0],
        "target": batch.target,
        "category_id": batch.category_id,
        "root_path": batch.root_path,
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
        "counts": counts,
        **counts,
    }


def _load_batches_or_exit(staging_root: Path) -> list[batch_state.BatchFile]:
    rows: list[batch_state.BatchFile] = []
    for path in sorted(batch_state.batches_dir(staging_root).glob("*.json")):
        try:
            rows.append(batch_state.load_batch(staging_root, path.stem))
        except StagingError as e:
            exit_for_error(e)
    rows.sort(key=lambda batch: batch.updated_at, reverse=True)
    return rows


def _load_batch_or_usage(
    ctx: typer.Context, staging_root: Path, batch_id: str
) -> batch_state.BatchFile:
    try:
        path = batch_state.batch_file_path(staging_root, batch_id)
    except StagingError as e:
        exit_for_error(e)
    if not path.is_file():
        if appctx.json_mode(ctx):
            output_json(
                {
                    "batch_id": batch_id,
                    "reason": "batch_file_missing",
                    "next": _BATCH_MISSING_NEXT,
                }
            )
        else:
            err(f"batch_file_missing: {batch_id}; {_BATCH_MISSING_NEXT}")
        raise typer.Exit(2) from None
    try:
        return batch_state.load_batch(staging_root, batch_id)
    except StagingError as e:
        exit_for_error(e)


def _reason_next(reason: str | None, *, batch: batch_state.BatchFile) -> str | None:
    if reason is None:
        return None
    if reason == "regressed":
        return "stage status OBJECT; reset or restart timestamp flow"
    if reason == "missing_record_ids":
        return f"stage timestamp batch {batch.target} --batch {batch.batch_id} --retry"
    if reason == "lock_contended":
        return f"Re-run --batch {batch.batch_id} after the other process exits"
    if reason.startswith("in_batch="):
        return f"stage timestamp batch status {reason.removeprefix('in_batch=')}"
    if reason.startswith("category_mismatch="):
        return (
            "Start a separate batch for that category or reset/recreate the "
            "staging session"
        )
    if reason in {"committed", "no_new_digests", "noop"}:
        return "None"
    if reason == "content_not_upgraded":
        return "Upgrade content first"
    if reason == "missing_metadata":
        return "Add staged metadata or rerun with --allow-missing-metadata"
    if reason == "sp_partial_results":
        return "Re-run --upgrade later"
    if reason in {
        "results_expired",
        "verify_job_failed",
        "transient_rpc_error",
    }:
        return "Re-run --upgrade"
    if reason == "transient_sp_error":
        return f"stage timestamp batch {batch.target} --batch {batch.batch_id} --retry"
    if reason == "reorg_deeper_than_min_conf":
        return "Retry after node catches up or investigate chain state"
    if reason == "attestation_mismatch":
        return "Operator inspection required"
    if reason == "batch_file_missing":
        return _BATCH_MISSING_NEXT
    if reason == "submit_pending":
        return f"stage timestamp batch {batch.target} --batch {batch.batch_id} --retry"
    return "Inspect batch status"


def _object_staging_dir(staging_root: Path, *, root_path: str, object_id: str) -> Path:
    return StagingStore(staging_root).staging_path(root_path, object_id)


def _load_object_ots(staging_root: Path, batch: batch_state.BatchFile, object_id: str):
    staging_dir = _object_staging_dir(
        staging_root, root_path=batch.root_path, object_id=object_id
    )
    if not has_ots(staging_dir):
        return None
    try:
        return load_ots(staging_dir)
    except StagingError:
        return None


def _batch_complete(batch: batch_state.BatchFile) -> bool:
    active = [row for row in batch.objects if row.status not in {"skipped", "failed"}]
    return bool(active) and all(row.status == "upgraded" for row in active)


def _submitted_count_from_batch(
    row: batch_state.BatchObject, batch: batch_state.BatchFile
) -> int:
    submitted: set[str] = set()
    wanted = set(row.digests)
    for chunk in batch.submissions:
        if chunk.record_ids is None:
            continue
        for digest, record_id in zip(chunk.digests, chunk.record_ids, strict=False):
            if digest in wanted and record_id is not None:
                submitted.add(digest)
    return len(submitted)


def _submitted_count_from_object(
    batch: batch_state.BatchFile, row: batch_state.BatchObject, state: OtsState
) -> int:
    wanted = set(row.digests)
    submitted: set[str] = set()
    if batch.target == "content":
        for submission in state.submissions:
            if submission.batch_id != batch.batch_id or submission.record_ids is None:
                continue
            for digest, record_id in zip(
                submission.digests, submission.record_ids, strict=False
            ):
                if digest in wanted and record_id is not None:
                    submitted.add(digest)
        return len(submitted)
    proof = state.inventory_proof
    if (
        proof is not None
        and proof.batch_id == batch.batch_id
        and proof.digest in wanted
        and proof.record_id is not None
    ):
        return 1
    return 0


def _object_record_ids_by_digest(
    batch: batch_state.BatchFile, row: batch_state.BatchObject, state: OtsState | None
) -> dict[str, str | None]:
    record_ids: dict[str, str | None] = dict.fromkeys(row.digests)
    if state is None:
        return record_ids
    wanted = set(row.digests)
    if batch.target == "content":
        for submission in state.submissions:
            if submission.batch_id != batch.batch_id or submission.record_ids is None:
                continue
            for digest, record_id in zip(
                submission.digests, submission.record_ids, strict=False
            ):
                if digest in wanted:
                    record_ids[digest] = record_id
        return record_ids
    proof = state.inventory_proof
    if (
        proof is not None
        and proof.batch_id == batch.batch_id
        and proof.digest in wanted
    ):
        record_ids[proof.digest] = proof.record_id
    return record_ids


def _batch_record_ids_by_digest(
    batch: batch_state.BatchFile, row: batch_state.BatchObject
) -> dict[str, str | None]:
    record_ids: dict[str, str | None] = dict.fromkeys(row.digests)
    wanted = set(row.digests)
    for chunk in batch.submissions:
        if chunk.record_ids is None:
            continue
        for digest, record_id in zip(chunk.digests, chunk.record_ids, strict=False):
            if digest in wanted:
                record_ids[digest] = record_id
    return record_ids


def _submit_status_by_digest(
    batch: batch_state.BatchFile, row: batch_state.BatchObject
) -> dict[str, str | None]:
    statuses: dict[str, str | None] = dict.fromkeys(row.digests)
    wanted = set(row.digests)
    for chunk in batch.submissions:
        for digest in chunk.digests:
            if digest in wanted:
                statuses[digest] = chunk.status
    return statuses


def _verify_status_by_digest(
    batch: batch_state.BatchFile, row: batch_state.BatchObject
) -> dict[str, str | None]:
    statuses: dict[str, str | None] = dict.fromkeys(row.digests)
    wanted = set(row.digests)
    for job in batch.verify_jobs:
        for digest in job.digests:
            if digest in wanted:
                statuses[digest] = job.status
    return statuses


def _verified_count_for_row(
    batch: batch_state.BatchFile, row: batch_state.BatchObject, state: OtsState | None
) -> int:
    if state is None:
        return 0
    wanted = set(row.digests)
    if batch.target == "content":
        return sum(
            1
            for digest in wanted
            if state.content_proofs.get(digest, None) is not None
            and state.content_proofs[digest].verified
        )
    proof = state.inventory_proof
    if proof is None or proof.digest not in wanted:
        return 0
    return int(proof.proof.verified)


def _verbose_digest_rows(
    batch: batch_state.BatchFile,
    row: batch_state.BatchObject,
    state: OtsState | None,
) -> list[dict]:
    object_ids = _object_record_ids_by_digest(batch, row, state)
    batch_ids = _batch_record_ids_by_digest(batch, row)
    submit_statuses = _submit_status_by_digest(batch, row)
    verify_statuses = _verify_status_by_digest(batch, row)
    verified: set[str] = set()
    if state is not None:
        if batch.target == "content":
            verified = {
                digest
                for digest in row.digests
                if state.content_proofs.get(digest, VerifiedProof()).verified
            }
        elif state.inventory_proof is not None and state.inventory_proof.proof.verified:
            verified = {state.inventory_proof.digest}
    return [
        {
            "digest": digest,
            "submitted": (object_ids.get(digest) or batch_ids.get(digest)) is not None,
            "verified": digest in verified,
            "record_id": object_ids.get(digest) or batch_ids.get(digest),
            "submit_status": submit_statuses[digest],
            "verify_status": verify_statuses[digest],
        }
        for digest in row.digests
    ]


def _verify_job_covers(
    batch: batch_state.BatchFile, row: batch_state.BatchObject
) -> bool:
    wanted = set(row.digests)
    return any(
        job.status == "IN_PROGRESS" and wanted.issubset(set(job.digests))
        for job in batch.verify_jobs
    )


def _next_for_row(
    batch: batch_state.BatchFile,
    row: batch_state.BatchObject,
    *,
    digests_submitted: int,
) -> str:
    reason = row.last_error or row.skipped_reason
    if row.status in {"failed", "skipped"} or reason is not None:
        if reason == "transient_sp_error" and row.status == "submitted":
            return "Re-run --upgrade"
        return _reason_next(reason, batch=batch) or "Inspect batch status"
    if _batch_complete(batch):
        return f"Batch complete; batch rm {batch.batch_id} to clean up"
    if row.status == "prepared":
        return f"Run --batch {batch.batch_id} to submit"
    if row.status == "submitted":
        if digests_submitted < len(row.digests):
            return f"Run --batch {batch.batch_id} --retry"
        if _verify_job_covers(batch, row):
            return "Re-run --upgrade later"
        return f"Run --batch {batch.batch_id} --upgrade"
    if row.status == "upgraded":
        return "Object complete"
    return "Inspect batch status"


def _batch_status_dict(
    staging_root: Path, batch: batch_state.BatchFile, *, verbose: bool = False
) -> dict:
    objects = []
    for row in batch.objects:
        state = _load_object_ots(staging_root, batch, row.object_id)
        phase = state.phase.name if state is not None else None
        batch_submitted = _submitted_count_from_batch(row, batch)
        object_submitted = (
            _submitted_count_from_object(batch, row, state) if state is not None else 0
        )
        digests_submitted = max(batch_submitted, object_submitted)
        object_data = {
            "object_id": row.object_id,
            "phase": phase,
            "batch_status": row.status,
            "digests_total": len(row.digests),
            "digests_verified": _verified_count_for_row(batch, row, state),
            "digests_submitted": digests_submitted,
            "last_error": row.last_error,
            "skipped_reason": row.skipped_reason,
            "next": _next_for_row(batch, row, digests_submitted=digests_submitted),
            "batch_id": batch.batch_id,
        }
        if verbose:
            object_data["digests"] = _verbose_digest_rows(batch, row, state)
        objects.append(object_data)
    return {
        "batch_id": batch.batch_id,
        "target": batch.target,
        "root_path": batch.root_path,
        "category_id": batch.category_id,
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
        "counts": _batch_counts(batch),
        "objects": objects,
        "submissions": [msgspec.to_builtins(row) for row in batch.submissions],
        "verify_jobs": [msgspec.to_builtins(row) for row in batch.verify_jobs],
    }


@batch_app.command("ls")
def timestamp_batch_ls(ctx: typer.Context) -> None:
    """List local batch timestamping state files."""
    staging_root = _stage_store(ctx)._root
    rows = [_batch_to_list_row(batch) for batch in _load_batches_or_exit(staging_root)]
    if appctx.json_mode(ctx):
        output_json(rows)
        return
    for row in rows:
        typer.echo(
            f"{row['short_batch_id']}  {row['target']}  "
            f"created={row['created_at']} updated={row['updated_at']}  "
            f"prepared={row['prepared']} submitted={row['submitted']} "
            f"upgraded={row['upgraded']} failed={row['failed']} "
            f"skipped={row['skipped']} total={row['total']}"
        )


@batch_app.command("status")
def timestamp_batch_status(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Argument(help="Batch ID.")],
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="Show per-digest submit/verify state."),
    ] = False,
) -> None:
    """Show local per-object status for a batch."""
    staging_root = _stage_store(ctx)._root
    batch = _load_batch_or_usage(ctx, staging_root, batch_id)
    data = _batch_status_dict(staging_root, batch, verbose=verbose)
    if appctx.json_mode(ctx):
        output_json(data)
        return
    _render_status_section("Batch")
    typer.echo(f"  ID: {batch.batch_id}")
    typer.echo(f"  Target: {batch.target}")
    typer.echo(f"  Category: {batch.category_id}")
    typer.echo(f"  Updated: {batch.updated_at}")
    _render_status_section("Objects", leading_blank=True)
    for row in data["objects"]:
        typer.echo(
            f"  {row['object_id']}  {row['batch_status']}  "
            f"{row['digests_verified']}/{row['digests_total']} verified  "
            f"Next: {row['next']}"
        )
        if verbose:
            for digest in row["digests"]:
                typer.echo(
                    f"    {digest['digest']}  submitted={digest['submitted']} "
                    f"verified={digest['verified']} "
                    f"submit={digest['submit_status']} "
                    f"verify={digest['verify_status']}"
                )


@batch_app.command("rm")
def timestamp_batch_rm(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Argument(help="Batch ID.")],
    force: Annotated[
        bool,
        typer.Option("--force", help="Delete the batch file even with open rows."),
    ] = False,
) -> None:
    """Delete a local batch file when cleanup is safe."""
    staging_root = _stage_store(ctx)._root
    batch = _load_batch_or_usage(ctx, staging_root, batch_id)
    open_rows = [
        row for row in batch.objects if row.status in {"prepared", "submitted"}
    ]
    if open_rows and not force:
        err(
            f"batch {batch_id} has non-terminal rows; use --force to delete "
            "the batch file only"
        )
        raise typer.Exit(1)
    batch_state.batch_file_path(staging_root, batch_id).unlink()
    typer.echo(f"removed batch {batch_id}")


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
        _render_watch_start(object_id)
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
                progress=lambda kind, data: _render_watch_progress(
                    kind, data, next_action="stage timestamp inventory"
                ),
            )
        except (ReorgDetectedError, AttestationMismatchError) as e:
            exit_for_error(e, message=f"halt: {e}")
        except SimpleProofError as e:
            exit_for_error(e)
        raise typer.Exit(exit_code)

    if upgrade:
        try:
            result = service._upgrade_pass(
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
            exit_for_error(e, message=f"halt: {e}")
        except SimpleProofError as e:
            exit_for_error(e)
        _render_content_upgrade_result(object_id, result)
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
                exit_for_error(e)
            except SimpleProofError as e:
                exit_for_error(e)
            except service.NoFailedSubmissionsError as e:
                typer.echo(str(e))
                return
        return

    with with_lock(resolved.staging_dir, timeout=service._LOCK_TIMEOUT_SECONDS):
        try:
            result = service.submit_content_locked(
                cfg,
                resolved.store,
                sr,
                resolved.root_path,
                object_id,
                resolved.staging_dir,
                sp_factory=preflight.sp_factory,
            )
        except PhaseError as e:
            exit_for_error(e)
        except SimpleProofError as e:
            exit_for_error(e)
        _render_status_section("Content")
        typer.echo(f"  Object: {object_id}")
        if result.submitted_count:
            typer.echo(
                f"  Submitted {result.submitted_count} "
                f"{_plural(result.submitted_count, 'digest')} to SimpleProof"
            )
        else:
            typer.echo("  Nothing to timestamp")

        _render_status_section("Workflow", leading_blank=True)
        if result.previous_phase is result.phase:
            typer.echo(f"  Phase: {result.phase.value}")
        else:
            typer.echo(
                f"  Phase: {result.previous_phase.value} -> {result.phase.value}"
            )
        if result.noop_advanced:
            typer.echo("  Next: stage timestamp inventory")
        else:
            typer.echo("  Next: stage timestamp content --upgrade --watch")


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
    allow_missing_metadata: Annotated[
        bool,
        typer.Option(
            "--allow-missing-metadata",
            help="Timestamp inventory even when staged version metadata is incomplete.",
        ),
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
    validate_inventory_metadata_flag(
        ctx,
        upgrade=upgrade,
        watch=watch_flag,
        retry=retry,
        allow_missing_metadata=allow_missing_metadata,
    )
    cfg = preflight.cfg
    resolved = preflight.resolved

    sr, _fs = open_root(appctx.root(ctx))

    if watch_flag:
        _render_watch_start(object_id)
        try:
            exit_code = watch._inventory_watch_loop(
                cfg,
                resolved.staging_dir,
                sp_factory=preflight.sp_factory,
                btc_factory=preflight.btc_factory,
                progress=lambda kind, data: _render_watch_progress(
                    kind, data, next_action="stage commit"
                ),
            )
        except (ReorgDetectedError, AttestationMismatchError) as e:
            exit_for_error(e, message=f"halt: {e}")
        except SimpleProofError as e:
            exit_for_error(e)
        except PhaseError as e:
            exit_for_error(e)
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
            exit_for_error(e, message=f"halt: {e}")
        except SimpleProofError as e:
            exit_for_error(e)
        except PhaseError as e:
            exit_for_error(e)
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
                exit_for_error(e)
            except SimpleProofError as e:
                exit_for_error(e)
            except service.InventoryProofMissingError as e:
                exit_for_error(e)
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
                allow_missing_metadata=allow_missing_metadata,
            )
        except PhaseError as e:
            exit_for_error(e, message=f"{e}; run 'stage timestamp content' first")
        except MissingStagedMetadataError as e:
            exit_for_error(e)
        except SimpleProofError as e:
            exit_for_error(e)


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
