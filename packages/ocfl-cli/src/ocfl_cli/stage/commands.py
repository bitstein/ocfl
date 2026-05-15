"""Typer callbacks for the stage command group.

Thin glue: parse options, resolve context, call into ``service`` /
``watch``, render results. Business logic lives in those modules.
"""

import contextlib
import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, cast

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
    BitcoinError,
    ConfigError,
    LockTimeoutError,
    MissingStagedMetadataError,
    PhaseError,
    ReorgDetectedError,
    SimpleProofBadRequest,
    SimpleProofError,
    SimpleProofTransient,
    StagingError,
)
from ocfl_cli.ots.bitcoin import (
    PendingVerification,
    VerifiedAttestation,
    verify_attestation,
)
from ocfl_cli.ots.simpleproof import _VERIFY_BATCH_CHUNK_SIZE
from ocfl_cli.stage import batch as batch_state
from ocfl_cli.stage import service, watch
from ocfl_cli.stage.fsm import Phase
from ocfl_cli.stage.ingest import ingest_file
from ocfl_cli.stage.schemas import StagedMetadata
from ocfl_cli.stage.state import (
    OtsState,
    Submission,
    VerifiedProof,
    has_ots,
    load_ots,
    now_utc_iso,
    ots_cache_path,
    plan_path,
    save_ots,
    with_lock,
)
from ocfl_cli.stage.store import StagingStore
from ocfl_lib import DigestAlgorithm
from ocfl_lib.update_plan import deserialize, serialize

if TYPE_CHECKING:
    from ocfl_cli._appctx import BtcRpcFactory, SpClientFactory
    from ocfl_cli.config import Config, SimpleProofConfig
    from ocfl_cli.ots.bitcoin import HashBlockStream


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


def _emit_batch_event(ctx: typer.Context, data: dict) -> None:
    if appctx.json_mode(ctx):
        typer.echo(json.dumps(data, separators=(",", ":")))


def _event_for_row(row: batch_state.BatchObject) -> str:
    if row.status == "prepared":
        return "object_prepared"
    if row.status == "submitted":
        return "object_submitted"
    if row.status == "upgraded":
        return "object_upgraded"
    if row.status == "skipped":
        return "object_skipped"
    return "object_failed"


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


def _batch_object_event(
    staging_root: Path, batch: batch_state.BatchFile, row: batch_state.BatchObject
) -> dict:
    state = _load_object_ots(staging_root, batch, row.object_id)
    batch_submitted = _submitted_count_from_batch(row, batch)
    object_submitted = (
        _submitted_count_from_object(batch, row, state) if state is not None else 0
    )
    digests_submitted = max(batch_submitted, object_submitted)
    return {
        "event": _event_for_row(row),
        "batch_id": batch.batch_id,
        "target": batch.target,
        "object_id": row.object_id,
        "status": row.status,
        "digests_total": len(row.digests),
        "digests_submitted": digests_submitted,
        "digests_verified": _verified_count_for_row(batch, row, state),
        "last_error": row.last_error,
        "skipped_reason": row.skipped_reason,
        "next": _next_for_row(batch, row, digests_submitted=digests_submitted),
    }


def _emit_batch_object_event(
    ctx: typer.Context,
    *,
    staging_root: Path,
    batch: batch_state.BatchFile,
    row: batch_state.BatchObject,
) -> None:
    _emit_batch_event(ctx, _batch_object_event(staging_root, batch, row))


def _emit_batch_object_changes(
    ctx: typer.Context,
    *,
    staging_root: Path,
    before: batch_state.BatchFile,
    after: batch_state.BatchFile,
) -> None:
    previous = {row.object_id: row for row in before.objects}
    for row in after.objects:
        old = previous.get(row.object_id)
        if old is None:
            _emit_batch_object_event(
                ctx, staging_root=staging_root, batch=after, row=row
            )
            continue
        old_event = _batch_object_event(staging_root, before, old)
        new_event = _batch_object_event(staging_root, after, row)
        if old_event != new_event:
            _emit_batch_event(ctx, new_event)


def _emit_batch_summary(ctx: typer.Context, batch: batch_state.BatchFile) -> None:
    _emit_batch_event(
        ctx,
        {
            "event": "summary",
            "batch_id": batch.batch_id,
            "target": batch.target,
            "category_id": batch.category_id,
            "root_path": batch.root_path,
            "counts": _batch_counts(batch),
        },
    )


def _read_objects_file(path: Path) -> list[str]:
    object_ids: list[str] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            object_ids.append(stripped)
    return object_ids


def _replace_batch_object(
    batch: batch_state.BatchFile, row: batch_state.BatchObject
) -> batch_state.BatchFile:
    rows = [
        existing for existing in batch.objects if existing.object_id != row.object_id
    ]
    rows.append(row)
    return msgspec.structs.replace(batch, objects=tuple(rows), updated_at=now_utc_iso())


def _object_has_other_batch(
    state: OtsState, batch_id: str, target: batch_state.BatchTarget
) -> str | None:
    if target == "inventory":
        proof = state.inventory_proof
        if (
            proof is not None
            and proof.batch_id is not None
            and proof.batch_id != batch_id
            and proof.record_id is None
        ):
            return proof.batch_id
        return None
    for submission in state.submissions:
        if (
            submission.status in {"submitting", "submitted"}
            and submission.batch_id is not None
            and submission.batch_id != batch_id
        ):
            return submission.batch_id
    return None


def _batch_command_tuple(target: str, batch_id: str) -> tuple[str, ...]:
    return ("stage", "timestamp", "batch", target, "--batch", batch_id)


def _prepare_batch_object(
    *,
    target: batch_state.BatchTarget,
    cfg,
    store: StagingStore,
    sr,
    root_path: str,
    staging_root: Path,
    batch: batch_state.BatchFile,
    object_id: str,
    allow_missing_metadata: bool,
) -> batch_state.BatchObject:
    staging_dir = store.staging_path(root_path, object_id)
    if not store.exists(root_path, object_id) or not has_ots(staging_dir):
        return batch_state.BatchObject(
            object_id=object_id, status="skipped", skipped_reason="committed"
        )
    try:
        with with_lock(staging_dir, timeout=service._LOCK_TIMEOUT_SECONDS):
            state = load_ots(staging_dir)
            if state.category_id != batch.category_id:
                return batch_state.BatchObject(
                    object_id=object_id,
                    status="skipped",
                    skipped_reason=f"category_mismatch={state.category_id}",
                )
            if (
                other := _object_has_other_batch(state, batch.batch_id, target)
            ) is not None:
                return batch_state.BatchObject(
                    object_id=object_id,
                    status="skipped",
                    skipped_reason=f"in_batch={other}",
                )
            if target == "content":
                prepared = service.prepare_content_submission_locked(
                    cfg, store, sr, root_path, object_id, staging_dir
                )
                if not prepared.digests:
                    return batch_state.BatchObject(
                        object_id=object_id,
                        status="skipped",
                        skipped_reason=(
                            "noop" if prepared.noop_advanced else "no_new_digests"
                        ),
                    )
                state = load_ots(staging_dir)
                submissions = list(state.submissions)
                submissions[-1] = msgspec.structs.replace(
                    submissions[-1],
                    batch_id=batch.batch_id,
                    record_ids=tuple(None for _ in prepared.digests),
                )
                save_ots(
                    staging_dir,
                    msgspec.structs.replace(state, submissions=tuple(submissions)),
                )
                return batch_state.BatchObject(
                    object_id=object_id, status="prepared", digests=prepared.digests
                )

            prepared = service.prepare_inventory_submission_locked(
                cfg,
                store,
                sr,
                root_path,
                object_id,
                staging_dir,
                allow_missing_metadata=allow_missing_metadata,
            )
            if prepared.digest is None:
                return batch_state.BatchObject(
                    object_id=object_id, status="skipped", skipped_reason="noop"
                )
            state = load_ots(staging_dir)
            if state.inventory_proof is None:
                return batch_state.BatchObject(
                    object_id=object_id, status="failed", last_error="regressed"
                )
            save_ots(
                staging_dir,
                msgspec.structs.replace(
                    state,
                    inventory_proof=msgspec.structs.replace(
                        state.inventory_proof,
                        batch_id=batch.batch_id,
                        record_id=None,
                    ),
                ),
            )
            if prepared.plan is not None:
                plan_path(staging_dir).write_bytes(serialize(prepared.plan))
            return batch_state.BatchObject(
                object_id=object_id, status="prepared", digests=(prepared.digest,)
            )
    except LockTimeoutError:
        return batch_state.BatchObject(
            object_id=object_id, status="skipped", skipped_reason="lock_contended"
        )
    except MissingStagedMetadataError:
        return batch_state.BatchObject(
            object_id=object_id, status="skipped", skipped_reason="missing_metadata"
        )
    except PhaseError:
        reason = "content_not_upgraded" if target == "inventory" else "no_new_digests"
        return batch_state.BatchObject(
            object_id=object_id, status="skipped", skipped_reason=reason
        )


def _ensure_submit_chunks(batch: batch_state.BatchFile) -> batch_state.BatchFile:
    if batch.submissions:
        return batch
    digests = sorted(
        {d for row in batch.objects if row.status == "prepared" for d in row.digests}
    )
    chunks = tuple(
        batch_state.BatchSubmission(
            chunk_index=i // service._SUBMIT_CHUNK_SIZE,
            digests=tuple(digests[i : i + service._SUBMIT_CHUNK_SIZE]),
            status="pending",
        )
        for i in range(0, len(digests), service._SUBMIT_CHUNK_SIZE)
    )
    return msgspec.structs.replace(batch, submissions=chunks, updated_at=now_utc_iso())


def _row_by_object(batch: batch_state.BatchFile) -> dict[str, batch_state.BatchObject]:
    return {row.object_id: row for row in batch.objects}


def _chunk_object_ids(
    batch: batch_state.BatchFile, chunk: batch_state.BatchSubmission
) -> list[str]:
    chunk_digests = set(chunk.digests)
    return [
        row.object_id
        for row in batch.objects
        if row.status in {"prepared", "submitted"}
        and chunk_digests.intersection(row.digests)
    ]


def _mirror_chunk_to_object(
    *,
    target: batch_state.BatchTarget,
    store: StagingStore,
    root_path: str,
    staging_root: Path,
    batch: batch_state.BatchFile,
    object_id: str,
    ids_by_digest: dict[str, str],
) -> bool:
    staging_dir = store.staging_path(root_path, object_id)
    with with_lock(staging_dir, timeout=service._LOCK_TIMEOUT_SECONDS):
        state = load_ots(staging_dir)
        if target == "content":
            changed: list[Submission] = []
            completed = False
            for submission in state.submissions:
                if submission.batch_id != batch.batch_id:
                    changed.append(submission)
                    continue
                record_ids = list(
                    submission.record_ids
                    if submission.record_ids is not None
                    else tuple(None for _ in submission.digests)
                )
                for i, digest in enumerate(submission.digests):
                    if digest in ids_by_digest:
                        record_ids[i] = ids_by_digest[digest]
                completed = all(record_id is not None for record_id in record_ids)
                changed.append(
                    msgspec.structs.replace(
                        submission,
                        record_ids=tuple(record_ids),
                        status="submitted" if completed else "submitting",
                    )
                )
            phase = Phase.CONTENT_PENDING if completed else state.phase
            save_ots(
                staging_dir,
                msgspec.structs.replace(state, submissions=tuple(changed), phase=phase),
            )
            return completed

        proof = state.inventory_proof
        if proof is None or proof.batch_id != batch.batch_id:
            return False
        record_id = ids_by_digest.get(proof.digest)
        if record_id is None:
            return proof.record_id is not None
        save_ots(
            staging_dir,
            msgspec.structs.replace(
                state,
                inventory_proof=msgspec.structs.replace(proof, record_id=record_id),
            ),
        )
        return True


def _submit_batch(
    *,
    target: batch_state.BatchTarget,
    cfg,
    store: StagingStore,
    staging_root: Path,
    batch: batch_state.BatchFile,
    sp_factory,
) -> batch_state.BatchFile:
    batch = _ensure_submit_chunks(batch)
    with batch_state.with_batch_lock(staging_root, batch.batch_id):
        batch_state.save_batch(staging_root, batch)
    sp = sp_factory(cfg)
    rows = _row_by_object(batch)
    submissions = list(batch.submissions)
    for index, chunk in enumerate(submissions):
        if chunk.status == "failed":
            continue
        if chunk.status == "submitted" and chunk.record_ids is not None:
            ids_by_digest = dict(zip(chunk.digests, chunk.record_ids, strict=True))
        else:
            chunk, rows, object_ids = _revalidate_submit_chunk(
                target=target,
                store=store,
                batch=batch,
                chunk=chunk,
                rows=rows,
            )
            if not object_ids:
                submissions[index] = msgspec.structs.replace(
                    chunk,
                    status="submitted",
                    record_ids=(),
                )
                batch = _save_batch_submit_progress(
                    staging_root, batch, rows, submissions
                )
                continue
            try:
                result = sp.submit_hashes(list(chunk.digests), batch.category_id)
            except SimpleProofError as e:
                submissions[index] = msgspec.structs.replace(
                    chunk,
                    status="failed",
                    attempts=chunk.attempts + 1,
                    last_error=str(e),
                )
                for object_id in object_ids:
                    rows[object_id] = msgspec.structs.replace(
                        rows[object_id], last_error="transient_sp_error"
                    )
                return _save_batch_submit_progress(
                    staging_root, batch, rows, submissions
                )
            if len(result.record_ids) != len(chunk.digests):
                failed = msgspec.structs.replace(
                    chunk,
                    status="failed",
                    attempts=chunk.attempts + 1,
                    last_error="missing_record_ids",
                )
                submissions[index] = failed
                for object_id in object_ids:
                    rows[object_id] = msgspec.structs.replace(
                        rows[object_id], last_error="missing_record_ids"
                    )
                continue
            ids_by_digest = dict(zip(chunk.digests, result.record_ids, strict=True))
            submissions[index] = msgspec.structs.replace(
                chunk,
                status="submitted",
                record_ids=result.record_ids,
                submitted_at=now_utc_iso(),
                attempts=chunk.attempts + 1,
            )
            batch = msgspec.structs.replace(
                batch,
                objects=tuple(rows.values()),
                submissions=tuple(submissions),
                updated_at=now_utc_iso(),
            )
            with batch_state.with_batch_lock(staging_root, batch.batch_id):
                batch_state.save_batch(staging_root, batch)

        for object_id in _chunk_object_ids(batch, chunk):
            if _mirror_chunk_to_object(
                target=target,
                store=store,
                root_path=batch.root_path,
                staging_root=staging_root,
                batch=batch,
                object_id=object_id,
                ids_by_digest=ids_by_digest,
            ):
                rows[object_id] = msgspec.structs.replace(
                    rows[object_id], status="submitted", last_error=None
                )
    return _save_batch_submit_progress(staging_root, batch, rows, submissions)


def _object_ids_for_digests(
    batch: batch_state.BatchFile, digests: tuple[str, ...]
) -> list[str]:
    wanted = set(digests)
    return [
        row.object_id
        for row in batch.objects
        if row.status in {"prepared", "submitted"} and wanted.intersection(row.digests)
    ]


def _collect_retry_record_ids(
    *,
    target: batch_state.BatchTarget,
    store: StagingStore,
    staging_root: Path,
    batch: batch_state.BatchFile,
    submissions: list[batch_state.BatchSubmission],
) -> tuple[dict[str, str], dict[str, batch_state.BatchObject]]:
    rows = _row_by_object(batch)
    ids_by_digest: dict[str, str] = {}
    mutable_indexes_by_digest: dict[str, list[int]] = {}
    for index, chunk in enumerate(submissions):
        if chunk.record_ids is None:
            continue
        for digest, record_id in zip(chunk.digests, chunk.record_ids, strict=False):
            if record_id is None:
                continue
            ids_by_digest[digest] = record_id
            if chunk.status != "failed":
                mutable_indexes_by_digest.setdefault(digest, []).append(index)
    for row in batch.objects:
        if row.status not in {"prepared", "submitted"}:
            continue
        staging_dir = store.staging_path(batch.root_path, row.object_id)
        with with_lock(staging_dir, timeout=service._LOCK_TIMEOUT_SECONDS):
            state = load_ots(staging_dir)
            for digest, record_id in _object_record_ids_by_digest(
                batch, row, state
            ).items():
                if record_id is not None:
                    existing = ids_by_digest.get(digest)
                    if existing is not None and existing != record_id:
                        for index in mutable_indexes_by_digest.get(digest, ()):
                            chunk = submissions[index]
                            if chunk.record_ids is None:
                                continue
                            record_ids = tuple(
                                record_id if chunk_digest == digest else chunk_record_id
                                for chunk_digest, chunk_record_id in zip(
                                    chunk.digests,
                                    chunk.record_ids,
                                    strict=False,
                                )
                            )
                            submissions[index] = msgspec.structs.replace(
                                chunk, record_ids=record_ids
                            )
                        ids_by_digest[digest] = record_id
                    else:
                        ids_by_digest[digest] = record_id
    for row in batch.objects:
        if rows[row.object_id].status not in {"prepared", "submitted"}:
            continue
        row_ids = {
            digest: record_id
            for digest in row.digests
            if (record_id := ids_by_digest.get(digest)) is not None
        }
        if row_ids and _mirror_chunk_to_object(
            target=target,
            store=store,
            root_path=batch.root_path,
            staging_root=staging_root,
            batch=batch,
            object_id=row.object_id,
            ids_by_digest=row_ids,
        ):
            rows[row.object_id] = msgspec.structs.replace(
                row, status="submitted", last_error=None
            )
    return ids_by_digest, rows


def _append_retry_chunk(
    submissions: list[batch_state.BatchSubmission],
    *,
    digests: tuple[str, ...],
    status: batch_state.BatchSubmissionStatus,
    record_ids: tuple[str, ...] | None = None,
    attempts: int = 0,
    last_error: str | None = None,
) -> batch_state.BatchSubmission:
    next_index = (
        max((submission.chunk_index for submission in submissions), default=-1) + 1
    )
    chunk = batch_state.BatchSubmission(
        chunk_index=next_index,
        digests=digests,
        status=status,
        record_ids=record_ids,
        submitted_at=now_utc_iso() if status == "submitted" else None,
        attempts=attempts,
        last_error=last_error,
    )
    submissions.append(chunk)
    return chunk


def _drive_batch_retry(
    ctx: typer.Context, *, target: batch_state.BatchTarget, batch_id: str | None
) -> None:
    if batch_id is None:
        err("--retry requires --batch")
        raise typer.Exit(2)
    cfg = require_config(ctx)
    require_simpleproof(ctx, cfg)
    store = _stage_store(ctx)
    staging_root = store._root
    batch = _load_batch_or_usage(ctx, staging_root, batch_id)
    if batch.target != target:
        err(f"batch {batch.batch_id} target is {batch.target!r}, not {target!r}")
        raise typer.Exit(2)
    with batch_state.with_driver_lease(
        staging_root,
        batch.batch_id,
        command=(*_batch_command_tuple(target, batch.batch_id), "--retry"),
    ):
        _emit_batch_resumed(ctx, batch)
        before_retry = batch
        batch = _retry_batch_submissions(
            target=target,
            cfg=cfg,
            store=store,
            staging_root=staging_root,
            batch=batch,
            sp_factory=appctx.sp_client_factory(ctx),
        )
        _emit_batch_object_changes(
            ctx, staging_root=staging_root, before=before_retry, after=batch
        )
    _emit_batch_summary(ctx, batch)
    if not appctx.json_mode(ctx):
        counts = _batch_counts(batch)
        typer.echo(
            f"batch retried: {batch.batch_id} prepared={counts['prepared']} "
            f"submitted={counts['submitted']} failed={counts['failed']}"
        )


def _missing_prepared_retry_digests(
    *,
    target: batch_state.BatchTarget,
    store: StagingStore,
    batch: batch_state.BatchFile,
    rows: dict[str, batch_state.BatchObject],
    ids_by_digest: dict[str, str],
    source_digests: set[str],
) -> tuple[str, ...]:
    missing: list[str] = []
    seen: set[str] = set(source_digests)
    for row in batch.objects:
        if rows[row.object_id].status != "prepared":
            continue
        staging_dir = store.staging_path(batch.root_path, row.object_id)
        with with_lock(staging_dir, timeout=service._LOCK_TIMEOUT_SECONDS):
            state = load_ots(staging_dir)
            object_ids = _object_record_ids_by_digest(batch, row, state)
        for digest in row.digests:
            if digest in seen:
                continue
            if ids_by_digest.get(digest) is None and object_ids.get(digest) is None:
                missing.append(digest)
                seen.add(digest)
    return tuple(missing)


def _save_retry_pre_post(
    staging_root: Path,
    batch: batch_state.BatchFile,
    rows: dict[str, batch_state.BatchObject],
    submissions: list[batch_state.BatchSubmission],
) -> batch_state.BatchFile:
    batch = msgspec.structs.replace(
        batch,
        objects=tuple(rows.values()),
        submissions=tuple(submissions),
        updated_at=now_utc_iso(),
    )
    with batch_state.with_batch_lock(staging_root, batch.batch_id):
        batch_state.save_batch(staging_root, batch)
    return batch


def _retry_batch_submissions(
    *,
    target: batch_state.BatchTarget,
    cfg,
    store: StagingStore,
    staging_root: Path,
    batch: batch_state.BatchFile,
    sp_factory,
) -> batch_state.BatchFile:
    batch = _ensure_submit_chunks(batch)
    submissions = list(batch.submissions)
    ids_by_digest, rows = _collect_retry_record_ids(
        target=target,
        store=store,
        staging_root=staging_root,
        batch=batch,
        submissions=submissions,
    )
    retry_sources = [
        (index, chunk)
        for index, chunk in enumerate(submissions)
        if chunk.status in {"failed", "pending"}
    ]
    source_digests = {
        digest for _index, chunk in retry_sources for digest in chunk.digests
    }
    for digest in _missing_prepared_retry_digests(
        target=target,
        store=store,
        batch=batch,
        rows=rows,
        ids_by_digest=ids_by_digest,
        source_digests=source_digests,
    ):
        retry_sources.append(
            (
                len(submissions),
                _append_retry_chunk(
                    submissions,
                    digests=(digest,),
                    status="pending",
                ),
            )
        )
        source_digests.add(digest)
    processed_digests: set[str] = set()
    sp = sp_factory(cfg)
    for source_index, source in retry_sources:
        source_digests = tuple(
            digest for digest in source.digests if digest not in processed_digests
        )
        if not source_digests:
            continue
        repaired = tuple(
            (digest, record_id)
            for digest in source_digests
            if (record_id := ids_by_digest.get(digest)) is not None
        )
        if repaired:
            if len(repaired) == len(source_digests) and source.status == "pending":
                repair_chunk = msgspec.structs.replace(
                    source,
                    status="submitted",
                    record_ids=tuple(record_id for _digest, record_id in repaired),
                    submitted_at=now_utc_iso(),
                )
                submissions[source_index] = repair_chunk
            else:
                repair_chunk = _append_retry_chunk(
                    submissions,
                    digests=tuple(digest for digest, _record_id in repaired),
                    status="submitted",
                    record_ids=tuple(record_id for _digest, record_id in repaired),
                )
            for object_id in _object_ids_for_digests(batch, repair_chunk.digests):
                if _mirror_chunk_to_object(
                    target=target,
                    store=store,
                    root_path=batch.root_path,
                    staging_root=staging_root,
                    batch=batch,
                    object_id=object_id,
                    ids_by_digest=dict(repaired),
                ):
                    rows[object_id] = msgspec.structs.replace(
                        rows[object_id], status="submitted", last_error=None
                    )
        to_post = tuple(
            digest for digest in source_digests if ids_by_digest.get(digest) is None
        )
        processed_digests.update(source_digests)
        if not to_post:
            continue
        retry_chunk = (
            msgspec.structs.replace(source, digests=to_post)
            if source.status == "pending"
            else batch_state.BatchSubmission(
                chunk_index=max(
                    (submission.chunk_index for submission in submissions), default=-1
                )
                + 1,
                digests=to_post,
                status="pending",
            )
        )
        retry_chunk, rows, object_ids = _revalidate_submit_chunk(
            target=target,
            store=store,
            batch=msgspec.structs.replace(batch, submissions=tuple(submissions)),
            chunk=retry_chunk,
            rows=rows,
        )
        if not object_ids:
            if source.status == "pending":
                submissions[source_index] = msgspec.structs.replace(
                    retry_chunk, status="submitted", record_ids=()
                )
            continue
        retry_chunk = msgspec.structs.replace(
            retry_chunk, attempts=retry_chunk.attempts + 1
        )
        if source.status == "pending":
            submissions[source_index] = retry_chunk
            retry_index = source_index
        else:
            submissions.append(retry_chunk)
            retry_index = len(submissions) - 1
        batch = _save_retry_pre_post(staging_root, batch, rows, submissions)
        try:
            result = sp.submit_hashes(list(retry_chunk.digests), batch.category_id)
        except SimpleProofError as e:
            submissions[retry_index] = msgspec.structs.replace(
                retry_chunk,
                status="failed",
                last_error=str(e),
            )
            for object_id in object_ids:
                rows[object_id] = msgspec.structs.replace(
                    rows[object_id], last_error="transient_sp_error"
                )
            return _save_batch_submit_progress(staging_root, batch, rows, submissions)
        if len(result.record_ids) != len(retry_chunk.digests):
            submissions[retry_index] = msgspec.structs.replace(
                retry_chunk,
                status="failed",
                last_error="missing_record_ids",
            )
            for object_id in object_ids:
                rows[object_id] = msgspec.structs.replace(
                    rows[object_id], last_error="missing_record_ids"
                )
            continue
        retry_ids = dict(zip(retry_chunk.digests, result.record_ids, strict=True))
        ids_by_digest.update(retry_ids)
        retry_chunk = msgspec.structs.replace(
            retry_chunk,
            status="submitted",
            record_ids=result.record_ids,
            submitted_at=now_utc_iso(),
        )
        submissions[retry_index] = retry_chunk
        for object_id in object_ids:
            if _mirror_chunk_to_object(
                target=target,
                store=store,
                root_path=batch.root_path,
                staging_root=staging_root,
                batch=batch,
                object_id=object_id,
                ids_by_digest=retry_ids,
            ):
                rows[object_id] = msgspec.structs.replace(
                    rows[object_id], status="submitted", last_error=None
                )
    return _save_batch_submit_progress(staging_root, batch, rows, submissions)


def _collect_recoverable_object_intents(
    store: StagingStore, batch: batch_state.BatchFile
) -> tuple[batch_state.BatchObject, ...]:
    known = {row.object_id for row in batch.objects}
    recovered: list[batch_state.BatchObject] = []
    for staging_dir in sorted(store._root.iterdir()):
        if not staging_dir.is_dir() or not has_ots(staging_dir):
            continue
        inventory_path = staging_dir / "inventory.json"
        if not inventory_path.is_file():
            continue
        try:
            raw_inventory = msgspec.json.decode(inventory_path.read_bytes())
        except OSError, msgspec.DecodeError:
            continue
        if not isinstance(raw_inventory, dict):
            continue
        object_id = raw_inventory.get("id")
        if not isinstance(object_id, str) or object_id in known:
            continue
        if store.staging_path(batch.root_path, object_id) != staging_dir:
            continue
        with with_lock(staging_dir, timeout=service._LOCK_TIMEOUT_SECONDS):
            state = load_ots(staging_dir)
            row = _recover_content_intent(batch, object_id, state)
            if batch.target == "inventory":
                row = _recover_inventory_intent(batch, object_id, state)
        if row is not None:
            recovered.append(row)
            known.add(object_id)
    return tuple(recovered)


def _merge_recovered_object_intents(
    batch: batch_state.BatchFile, recovered: tuple[batch_state.BatchObject, ...]
) -> batch_state.BatchFile:
    if not recovered:
        return batch
    known = {row.object_id for row in batch.objects}
    merged = list(batch.objects)
    for row in recovered:
        if row.object_id not in known:
            merged.append(row)
            known.add(row.object_id)
    if len(merged) == len(batch.objects):
        return batch
    return msgspec.structs.replace(
        batch, objects=tuple(merged), updated_at=now_utc_iso()
    )


def _recover_content_intent(
    batch: batch_state.BatchFile, object_id: str, state: OtsState
) -> batch_state.BatchObject | None:
    for submission in state.submissions:
        if submission.batch_id != batch.batch_id:
            continue
        submitted = submission.record_ids is not None and all(
            record_id is not None for record_id in submission.record_ids
        )
        return batch_state.BatchObject(
            object_id=object_id,
            status="submitted" if submitted else "prepared",
            digests=submission.digests,
        )
    return None


def _recover_inventory_intent(
    batch: batch_state.BatchFile, object_id: str, state: OtsState
) -> batch_state.BatchObject | None:
    proof = state.inventory_proof
    if proof is None or proof.batch_id != batch.batch_id:
        return None
    return batch_state.BatchObject(
        object_id=object_id,
        status="submitted" if proof.record_id is not None else "prepared",
        digests=(proof.digest,),
    )


def _save_batch_submit_progress(
    staging_root: Path,
    batch: batch_state.BatchFile,
    rows: dict[str, batch_state.BatchObject],
    submissions: list[batch_state.BatchSubmission],
) -> batch_state.BatchFile:
    batch = msgspec.structs.replace(
        batch,
        objects=tuple(rows.values()),
        submissions=tuple(submissions),
        updated_at=now_utc_iso(),
    )
    with batch_state.with_batch_lock(staging_root, batch.batch_id):
        batch_state.save_batch(staging_root, batch)
    return batch


def _revalidate_submit_chunk(
    *,
    target: batch_state.BatchTarget,
    store: StagingStore,
    batch: batch_state.BatchFile,
    chunk: batch_state.BatchSubmission,
    rows: dict[str, batch_state.BatchObject],
) -> tuple[
    batch_state.BatchSubmission,
    dict[str, batch_state.BatchObject],
    list[str],
]:
    valid_digests = set(chunk.digests)
    valid_object_ids: list[str] = []
    for object_id in _chunk_object_ids(batch, chunk):
        staging_dir = store.staging_path(batch.root_path, object_id)
        with with_lock(staging_dir, timeout=service._LOCK_TIMEOUT_SECONDS):
            state = load_ots(staging_dir)
            if state.category_id != batch.category_id:
                rows[object_id] = msgspec.structs.replace(
                    rows[object_id], status="failed", last_error="regressed"
                )
                valid_digests.difference_update(rows[object_id].digests)
                continue
            if target == "content":
                row_digests = _revalidate_content_submission(
                    state, batch.batch_id, rows[object_id]
                )
            else:
                row_digests = _revalidate_inventory_submission(
                    state, batch.batch_id, rows[object_id]
                )
            if row_digests is None:
                rows[object_id] = msgspec.structs.replace(
                    rows[object_id], status="failed", last_error="regressed"
                )
                valid_digests.difference_update(rows[object_id].digests)
                continue
            chunk_row_digests = set(rows[object_id].digests).intersection(chunk.digests)
            already_recorded = chunk_row_digests - row_digests
            valid_digests.difference_update(already_recorded)
            if chunk_row_digests.intersection(valid_digests):
                valid_object_ids.append(object_id)
    filtered = tuple(digest for digest in chunk.digests if digest in valid_digests)
    if filtered != chunk.digests:
        chunk = msgspec.structs.replace(chunk, digests=filtered)
    return chunk, rows, valid_object_ids


def _revalidate_content_submission(
    state: OtsState, batch_id: str, row: batch_state.BatchObject
) -> set[str] | None:
    for submission in state.submissions:
        if submission.batch_id != batch_id:
            continue
        if not set(row.digests).issubset(set(submission.digests)):
            return None
        record_ids = (
            submission.record_ids
            if submission.record_ids is not None
            else tuple(None for _ in submission.digests)
        )
        return {
            digest
            for digest, record_id in zip(submission.digests, record_ids, strict=True)
            if digest in row.digests and record_id is None
        }
    return None


def _revalidate_inventory_submission(
    state: OtsState, batch_id: str, row: batch_state.BatchObject
) -> set[str] | None:
    proof = state.inventory_proof
    if proof is None or proof.batch_id != batch_id or proof.digest not in row.digests:
        return None
    if proof.record_id is not None:
        return set()
    return {proof.digest}


def _content_digest_verified(state: OtsState, digest: str) -> bool:
    proof = state.content_proofs.get(digest)
    return proof is not None and proof.verified


def _inventory_digest_verified(state: OtsState, digest: str) -> bool:
    proof = state.inventory_proof
    return proof is not None and proof.digest == digest and proof.proof.verified


def _batch_digest_verified(
    *, target: batch_state.BatchTarget, state: OtsState, digest: str
) -> bool:
    if target == "content":
        return _content_digest_verified(state, digest)
    return _inventory_digest_verified(state, digest)


def _covered_verify_digests(batch: batch_state.BatchFile) -> set[str]:
    return {
        digest
        for job in batch.verify_jobs
        if job.status in {"IN_PROGRESS", "COMPLETED"}
        for digest in job.digests
    }


def _batch_unverified_submitted_digests(
    *,
    target: batch_state.BatchTarget,
    store: StagingStore,
    batch: batch_state.BatchFile,
) -> tuple[str, ...]:
    covered = _covered_verify_digests(batch)
    pending: list[str] = []
    seen: set[str] = set()
    for row in batch.objects:
        if row.status != "submitted":
            continue
        staging_dir = store.staging_path(batch.root_path, row.object_id)
        with with_lock(staging_dir, timeout=service._LOCK_TIMEOUT_SECONDS):
            state = load_ots(staging_dir)
            for digest in row.digests:
                if digest in seen or digest in covered:
                    continue
                if not _batch_digest_verified(
                    target=target, state=state, digest=digest
                ):
                    pending.append(digest)
                    seen.add(digest)
    return tuple(pending)


def _save_batch_upgrade(
    staging_root: Path, batch: batch_state.BatchFile
) -> batch_state.BatchFile:
    batch = msgspec.structs.replace(batch, updated_at=now_utc_iso())
    with batch_state.with_batch_lock(staging_root, batch.batch_id):
        batch_state.save_batch(staging_root, batch)
    return batch


def _submit_batch_verify_jobs(
    *,
    target: batch_state.BatchTarget,
    cfg,
    store: StagingStore,
    staging_root: Path,
    batch: batch_state.BatchFile,
    sp_factory,
) -> batch_state.BatchFile:
    sp = sp_factory(cfg)
    digests = _batch_unverified_submitted_digests(
        target=target, store=store, batch=batch
    )
    if not digests:
        return batch
    jobs = list(batch.verify_jobs)
    for i in range(0, len(digests), _VERIFY_BATCH_CHUNK_SIZE):
        chunk = tuple(digests[i : i + _VERIFY_BATCH_CHUNK_SIZE])
        try:
            job_id = sp.verify_batch(list(chunk), batch.category_id)
        except SimpleProofTransient as e:
            failed = batch_state.BatchVerifyJob(
                job_id=f"failed-{len(jobs) + 1}",
                digests=chunk,
                status="FAILED",
                submitted_at=now_utc_iso(),
                attempts=1,
                last_error=str(e),
            )
            jobs.append(failed)
            rows = tuple(
                msgspec.structs.replace(row, last_error="transient_sp_error")
                if row.status == "submitted" and set(row.digests).intersection(chunk)
                else row
                for row in batch.objects
            )
            batch = msgspec.structs.replace(
                batch, objects=rows, verify_jobs=tuple(jobs)
            )
            return _save_batch_upgrade(staging_root, batch)
        jobs.append(
            batch_state.BatchVerifyJob(
                job_id=job_id,
                digests=chunk,
                status="IN_PROGRESS",
                submitted_at=now_utc_iso(),
                attempts=1,
            )
        )
        batch = msgspec.structs.replace(batch, verify_jobs=tuple(jobs))
        batch = _save_batch_upgrade(staging_root, batch)
    return batch


def _poll_batch_verify_jobs(
    *, cfg, staging_root: Path, batch: batch_state.BatchFile, sp_factory
) -> batch_state.BatchFile:
    sp = sp_factory(cfg)
    jobs: list[batch_state.BatchVerifyJob] = []
    rows = list(batch.objects)
    changed = False
    for job in batch.verify_jobs:
        if job.status != "IN_PROGRESS":
            jobs.append(job)
            continue
        try:
            poll = sp.poll_job(job.job_id)
        except SimpleProofTransient as e:
            jobs.append(
                msgspec.structs.replace(
                    job,
                    status="FAILED",
                    completed_at=now_utc_iso(),
                    last_error=str(e),
                )
            )
            affected = set(job.digests)
            rows = [
                msgspec.structs.replace(row, last_error="transient_sp_error")
                if row.status == "submitted" and affected.intersection(row.digests)
                else row
                for row in rows
            ]
            changed = True
            continue
        except SimpleProofError as e:
            jobs.append(
                msgspec.structs.replace(
                    job,
                    status="FAILED",
                    completed_at=now_utc_iso(),
                    last_error=str(e),
                )
            )
            affected = set(job.digests)
            rows = [
                msgspec.structs.replace(row, last_error="verify_job_failed")
                if row.status == "submitted" and affected.intersection(row.digests)
                else row
                for row in rows
            ]
            changed = True
            continue
        status = (
            poll.status
            if poll.status in {"IN_PROGRESS", "COMPLETED", "FAILED", "EXPIRED"}
            else "IN_PROGRESS"
        )
        updated = msgspec.structs.replace(
            job,
            status=status,
            results_url=poll.results_url or job.results_url,
            completed_at=(
                now_utc_iso() if status in {"COMPLETED", "FAILED", "EXPIRED"} else None
            ),
            last_error=(
                "verify_job_failed"
                if status == "FAILED"
                else "results_expired"
                if status == "EXPIRED"
                else job.last_error
            ),
        )
        jobs.append(updated)
        changed = changed or updated != job
        if status == "FAILED":
            affected = set(job.digests)
            rows = [
                msgspec.structs.replace(row, last_error="verify_job_failed")
                if row.status == "submitted" and affected.intersection(row.digests)
                else row
                for row in rows
            ]
        if status == "EXPIRED":
            affected = set(job.digests)
            rows = [
                msgspec.structs.replace(row, last_error="results_expired")
                if row.status == "submitted" and affected.intersection(row.digests)
                else row
                for row in rows
            ]
    if not changed:
        return batch
    return _save_batch_upgrade(
        staging_root,
        msgspec.structs.replace(batch, objects=tuple(rows), verify_jobs=tuple(jobs)),
    )


def _content_row_upgraded(state: OtsState, row: batch_state.BatchObject) -> bool:
    return all(_content_digest_verified(state, digest) for digest in row.digests)


def _inventory_row_upgraded(state: OtsState, row: batch_state.BatchObject) -> bool:
    return all(_inventory_digest_verified(state, digest) for digest in row.digests)


def _ingest_batch_content_downloads(
    *,
    store: StagingStore,
    sr,
    batch: batch_state.BatchFile,
    row: batch_state.BatchObject,
    downloads: dict[str, bytes],
    btc,
    min_confirmations: int,
) -> tuple[batch_state.BatchObject, bool]:
    staging_dir = store.staging_path(batch.root_path, row.object_id)
    with with_lock(staging_dir, timeout=service._LOCK_TIMEOUT_SECONDS):
        state = load_ots(staging_dir)
        inv = store.load_inventory(batch.root_path, row.object_id)
        alg = DigestAlgorithm.from_ocfl_name(inv.digest_algorithm)
        head_state = {d: list(ps) for d, ps in inv.versions[inv.head].state.items()}
        for digest in row.digests:
            ots_bytes = downloads.get(digest)
            if ots_bytes is None or _content_digest_verified(state, digest):
                continue
            state, inv = service._verify_and_ingest_digest(
                digest,
                ots_bytes,
                state,
                inv,
                head_state,
                alg,
                btc=btc,
                store=store,
                root_path=batch.root_path,
                object_id=row.object_id,
                staging_dir=staging_dir,
                min_confirmations=min_confirmations,
            )
        ready, _unverified_pending, _orphans = service._phase_advance_ready(
            state, store, sr, batch.root_path, row.object_id
        )
        if ready and state.phase is not Phase.CONTENT_UPGRADED:
            state = msgspec.structs.replace(state, phase=Phase.CONTENT_UPGRADED)
            save_ots(staging_dir, state)
        if _content_row_upgraded(state, row):
            return (
                msgspec.structs.replace(row, status="upgraded", last_error=None),
                True,
            )
    return row, False


def _ingest_batch_inventory_downloads(
    *,
    store: StagingStore,
    sr,
    batch: batch_state.BatchFile,
    row: batch_state.BatchObject,
    downloads: dict[str, bytes],
    btc,
    min_confirmations: int,
) -> tuple[batch_state.BatchObject, bool]:
    staging_dir = store.staging_path(batch.root_path, row.object_id)
    with with_lock(staging_dir, timeout=service._LOCK_TIMEOUT_SECONDS):
        state = load_ots(staging_dir)
        proof = state.inventory_proof
        if proof is None or proof.batch_id != batch.batch_id:
            return (
                msgspec.structs.replace(row, status="failed", last_error="regressed"),
                True,
            )
        if not _inventory_plan_matches_submission(
            store=store,
            sr=sr,
            batch=batch,
            row=row,
            staging_dir=staging_dir,
            state=state,
        ):
            return (
                msgspec.structs.replace(row, status="failed", last_error="regressed"),
                True,
            )
        ots_bytes = downloads.get(proof.digest)
        if ots_bytes is None or proof.proof.verified:
            return row, False
        state = service._verify_inventory_digest(
            state, ots_bytes, btc, staging_dir, min_confirmations
        )
        if _inventory_row_upgraded(state, row):
            return (
                msgspec.structs.replace(row, status="upgraded", last_error=None),
                True,
            )
    return row, False


def _inventory_plan_matches_submission(
    *,
    store: StagingStore,
    sr,
    batch: batch_state.BatchFile,
    row: batch_state.BatchObject,
    staging_dir: Path,
    state: OtsState,
) -> bool:
    proof = state.inventory_proof
    if proof is None or proof.digest not in row.digests:
        return False
    path = plan_path(staging_dir)
    if not path.is_file():
        return False
    prior_plan = deserialize(path.read_bytes())
    prior_inv = prior_plan.new_inv.inventory
    prior_created = prior_inv.versions[prior_inv.head].created
    metadata = store.load_metadata(batch.root_path, row.object_id)
    plan, _content_source = service.build_inventory_plan(
        store,
        sr,
        batch.root_path,
        row.object_id,
        message=metadata.message,
        user=service._version_user_from_staged(metadata),
        created=prior_created,
    )
    if plan.new_inv.digest != proof.digest:
        return False
    path.write_bytes(serialize(plan))
    return True


def _ingest_batch_verify_jobs(
    *,
    target: batch_state.BatchTarget,
    cfg,
    store: StagingStore,
    sr,
    staging_root: Path,
    batch: batch_state.BatchFile,
    sp_factory,
    btc_factory,
) -> batch_state.BatchFile:
    if cfg.bitcoin is None:
        raise RuntimeError("bitcoin config missing")
    sp = sp_factory(cfg)
    btc = btc_factory(cfg)
    min_confirmations = cfg.bitcoin.confirmations
    rows = {row.object_id: row for row in batch.objects}
    jobs = list(batch.verify_jobs)
    changed = False
    for index, job in enumerate(jobs):
        if job.status != "COMPLETED" or job.results_url is None:
            continue
        try:
            downloads = sp.download_results(job.results_url, set(job.digests))
        except SimpleProofBadRequest as e:
            jobs[index] = msgspec.structs.replace(
                job, status="EXPIRED", last_error=str(e)
            )
            affected = set(job.digests)
            rows = {
                object_id: (
                    msgspec.structs.replace(row, last_error="results_expired")
                    if row.status == "submitted" and affected.intersection(row.digests)
                    else row
                )
                for object_id, row in rows.items()
            }
            changed = True
            continue
        except SimpleProofTransient as e:
            jobs[index] = msgspec.structs.replace(
                job, status="FAILED", last_error=str(e)
            )
            affected = set(job.digests)
            rows = {
                object_id: (
                    msgspec.structs.replace(row, last_error="transient_sp_error")
                    if row.status == "submitted" and affected.intersection(row.digests)
                    else row
                )
                for object_id, row in rows.items()
            }
            changed = True
            continue
        except AttestationMismatchError:
            jobs[index] = msgspec.structs.replace(
                job, status="COMPLETED", last_error="attestation_mismatch"
            )
            affected = set(job.digests)
            rows = {
                object_id: (
                    msgspec.structs.replace(
                        row, status="failed", last_error="attestation_mismatch"
                    )
                    if row.status == "submitted" and affected.intersection(row.digests)
                    else row
                )
                for object_id, row in rows.items()
            }
            changed = True
            continue
        for row in list(rows.values()):
            if row.status != "submitted" or not set(row.digests).intersection(
                downloads
            ):
                continue
            try:
                if target == "content":
                    new_row, row_changed = _ingest_batch_content_downloads(
                        store=store,
                        sr=sr,
                        batch=batch,
                        row=row,
                        downloads=downloads,
                        btc=btc,
                        min_confirmations=min_confirmations,
                    )
                else:
                    new_row, row_changed = _ingest_batch_inventory_downloads(
                        store=store,
                        sr=sr,
                        batch=batch,
                        row=row,
                        downloads=downloads,
                        btc=btc,
                        min_confirmations=min_confirmations,
                    )
            except AttestationMismatchError:
                new_row = msgspec.structs.replace(
                    row, status="failed", last_error="attestation_mismatch"
                )
                row_changed = True
            except ReorgDetectedError:
                new_row = msgspec.structs.replace(
                    row, status="failed", last_error="reorg_deeper_than_min_conf"
                )
                row_changed = True
            except BitcoinError:
                new_row = msgspec.structs.replace(row, last_error="transient_rpc_error")
                row_changed = True
            if row_changed:
                rows[row.object_id] = new_row
                changed = True
    if not changed:
        return batch
    return _save_batch_upgrade(
        staging_root,
        msgspec.structs.replace(
            batch,
            objects=tuple(rows.values()),
            verify_jobs=tuple(jobs),
        ),
    )


def _reconcile_upgraded_batch_rows(
    *,
    target: batch_state.BatchTarget,
    store: StagingStore,
    staging_root: Path,
    batch: batch_state.BatchFile,
) -> batch_state.BatchFile:
    rows: list[batch_state.BatchObject] = []
    changed = False
    for row in batch.objects:
        if row.status != "submitted":
            rows.append(row)
            continue
        staging_dir = store.staging_path(batch.root_path, row.object_id)
        with with_lock(staging_dir, timeout=service._LOCK_TIMEOUT_SECONDS):
            state = load_ots(staging_dir)
            upgraded = (
                _content_row_upgraded(state, row)
                if target == "content"
                else _inventory_row_upgraded(state, row)
            )
        if upgraded:
            rows.append(
                msgspec.structs.replace(row, status="upgraded", last_error=None)
            )
            changed = True
        else:
            rows.append(row)
    if not changed:
        return batch
    return _save_batch_upgrade(
        staging_root, msgspec.structs.replace(batch, objects=tuple(rows))
    )


def _drive_batch_upgrade(
    ctx: typer.Context, *, target: batch_state.BatchTarget, batch_id: str | None
) -> None:
    if batch_id is None:
        err("--upgrade requires --batch")
        raise typer.Exit(2)
    cfg = require_config(ctx)
    require_simpleproof(ctx, cfg)
    require_bitcoin(ctx, cfg)
    store = _stage_store(ctx)
    staging_root = store._root
    batch = _load_batch_or_usage(ctx, staging_root, batch_id)
    if batch.target != target:
        err(f"batch {batch.batch_id} target is {batch.target!r}, not {target!r}")
        raise typer.Exit(2)
    sr, _fs = open_root(appctx.root(ctx))
    with batch_state.with_driver_lease(
        staging_root,
        batch.batch_id,
        command=(*_batch_command_tuple(target, batch.batch_id), "--upgrade"),
    ):
        _emit_batch_resumed(ctx, batch)
        before_upgrade = batch
        batch = _run_batch_upgrade_pass(
            target=target,
            cfg=cfg,
            store=store,
            sr=sr,
            staging_root=staging_root,
            batch=batch,
            sp_factory=appctx.sp_client_factory(ctx),
            btc_factory=appctx.btc_rpc_factory(ctx),
        )
        _emit_batch_object_changes(
            ctx, staging_root=staging_root, before=before_upgrade, after=batch
        )
    _emit_batch_summary(ctx, batch)
    if not appctx.json_mode(ctx):
        counts = _batch_counts(batch)
        typer.echo(
            f"batch upgraded: {batch.batch_id} submitted={counts['submitted']} "
            f"upgraded={counts['upgraded']} failed={counts['failed']}"
        )


def _run_batch_upgrade_pass(
    *,
    target: batch_state.BatchTarget,
    cfg,
    store: StagingStore,
    sr,
    staging_root: Path,
    batch: batch_state.BatchFile,
    sp_factory,
    btc_factory,
) -> batch_state.BatchFile:
    batch = _submit_batch_verify_jobs(
        target=target,
        cfg=cfg,
        store=store,
        staging_root=staging_root,
        batch=batch,
        sp_factory=sp_factory,
    )
    batch = _reconcile_upgraded_batch_rows(
        target=target,
        store=store,
        staging_root=staging_root,
        batch=batch,
    )
    batch = _poll_batch_verify_jobs(
        cfg=cfg,
        staging_root=staging_root,
        batch=batch,
        sp_factory=sp_factory,
    )
    batch = _ingest_batch_verify_jobs(
        target=target,
        cfg=cfg,
        store=store,
        sr=sr,
        staging_root=staging_root,
        batch=batch,
        sp_factory=sp_factory,
        btc_factory=btc_factory,
    )
    return _reconcile_upgraded_batch_rows(
        target=target,
        store=store,
        staging_root=staging_root,
        batch=batch,
    )


def _batch_has_submitted_subset(batch: batch_state.BatchFile) -> bool:
    return any(row.status == "submitted" for row in batch.objects)


def _batch_watch_complete(batch: batch_state.BatchFile) -> bool:
    return bool(batch.objects) and all(
        row.status in {"upgraded", "failed", "skipped"} for row in batch.objects
    )


def _batch_watch_successful(batch: batch_state.BatchFile) -> bool:
    return _batch_watch_complete(batch) and any(
        row.status == "upgraded" for row in batch.objects
    )


def _reverify_batch_content_row(
    *,
    btc,
    staging_dir: Path,
    state: OtsState,
    row: batch_state.BatchObject,
    min_confirmations: int,
) -> tuple[OtsState, set[str], set[str]]:
    proofs = dict(state.content_proofs)
    reset: set[str] = set()
    reorg: set[str] = set()
    for digest in row.digests:
        proof = proofs.get(digest)
        if proof is None or not proof.verified:
            continue
        cache = ots_cache_path(staging_dir, digest)
        if not cache.is_file():
            continue
        try:
            result = verify_attestation(
                rpc=btc,
                ots_bytes=cache.read_bytes(),
                expected_digest=bytes.fromhex(digest),
                min_confirmations=min_confirmations,
            )
        except AttestationMismatchError:
            proofs[digest] = VerifiedProof(ots_path=proof.ots_path, verified=False)
            reset.add(digest)
            continue
        except ReorgDetectedError:
            proofs[digest] = VerifiedProof(ots_path=proof.ots_path, verified=False)
            reorg.add(digest)
            continue
        match result:
            case VerifiedAttestation(block_height=h, confirmations=c):
                proofs[digest] = VerifiedProof(
                    ots_path=proof.ots_path,
                    verified=True,
                    block_height=h,
                    block_hash=result.block_hash,
                    confirmations_at_verify=c,
                    verified_at=proof.verified_at,
                )
            case PendingVerification():
                proofs[digest] = VerifiedProof(ots_path=proof.ots_path, verified=False)
                reset.add(digest)
    if proofs == state.content_proofs:
        return state, reset, reorg
    return msgspec.structs.replace(state, content_proofs=proofs), reset, reorg


def _reverify_batch_inventory_row(
    *,
    btc,
    staging_dir: Path,
    state: OtsState,
    row: batch_state.BatchObject,
    min_confirmations: int,
) -> tuple[OtsState, set[str], set[str]]:
    inv = state.inventory_proof
    if inv is None or inv.digest not in row.digests or not inv.proof.verified:
        return state, set(), set()
    cache = ots_cache_path(staging_dir, inv.digest)
    if not cache.is_file():
        return state, set(), set()
    try:
        result = verify_attestation(
            rpc=btc,
            ots_bytes=cache.read_bytes(),
            expected_digest=bytes.fromhex(inv.digest),
            min_confirmations=min_confirmations,
        )
    except AttestationMismatchError:
        proof = VerifiedProof(ots_path=inv.proof.ots_path, verified=False)
        return (
            msgspec.structs.replace(
                state, inventory_proof=msgspec.structs.replace(inv, proof=proof)
            ),
            {inv.digest},
            set(),
        )
    except ReorgDetectedError:
        proof = VerifiedProof(ots_path=inv.proof.ots_path, verified=False)
        return (
            msgspec.structs.replace(
                state, inventory_proof=msgspec.structs.replace(inv, proof=proof)
            ),
            set(),
            {inv.digest},
        )
    match result:
        case VerifiedAttestation(block_height=h, confirmations=c):
            proof = VerifiedProof(
                ots_path=inv.proof.ots_path,
                verified=True,
                block_height=h,
                block_hash=result.block_hash,
                confirmations_at_verify=c,
                verified_at=inv.proof.verified_at,
            )
            return (
                msgspec.structs.replace(
                    state, inventory_proof=msgspec.structs.replace(inv, proof=proof)
                ),
                set(),
                set(),
            )
        case PendingVerification():
            proof = VerifiedProof(ots_path=inv.proof.ots_path, verified=False)
            return (
                msgspec.structs.replace(
                    state, inventory_proof=msgspec.structs.replace(inv, proof=proof)
                ),
                {inv.digest},
                set(),
            )


def _reset_batch_row_phase(
    target: batch_state.BatchTarget, state: OtsState
) -> OtsState:
    phase = Phase.CONTENT_PENDING if target == "content" else Phase.INVENTORY_PENDING
    return msgspec.structs.replace(state, phase=phase)


def _reverify_batch_watch_rows(
    *,
    target: batch_state.BatchTarget,
    cfg,
    store: StagingStore,
    staging_root: Path,
    batch: batch_state.BatchFile,
    btc,
) -> batch_state.BatchFile:
    if cfg.bitcoin is None:
        raise RuntimeError("bitcoin config missing")
    reset: set[str] = set()
    rows: list[batch_state.BatchObject] = []
    changed_rows = False
    for row in batch.objects:
        if row.status not in {"submitted", "upgraded"}:
            rows.append(row)
            continue
        staging_dir = store.staging_path(batch.root_path, row.object_id)
        with with_lock(staging_dir, timeout=service._LOCK_TIMEOUT_SECONDS):
            state = load_ots(staging_dir)
            if target == "content":
                state, row_reset, row_reorg = _reverify_batch_content_row(
                    btc=btc,
                    staging_dir=staging_dir,
                    state=state,
                    row=row,
                    min_confirmations=cfg.bitcoin.confirmations,
                )
            else:
                state, row_reset, row_reorg = _reverify_batch_inventory_row(
                    btc=btc,
                    staging_dir=staging_dir,
                    state=state,
                    row=row,
                    min_confirmations=cfg.bitcoin.confirmations,
                )
            if row_reorg:
                state = _reset_batch_row_phase(target, state)
                row = msgspec.structs.replace(
                    row, status="failed", last_error="reorg_deeper_than_min_conf"
                )
                changed_rows = True
            if row_reset:
                reset.update(row_reset)
                state = _reset_batch_row_phase(target, state)
                if row.status == "upgraded":
                    row = msgspec.structs.replace(
                        row, status="submitted", last_error=None
                    )
                    changed_rows = True
            save_ots(staging_dir, state)
        rows.append(row)
    if not reset:
        if not changed_rows:
            return batch
        return _save_batch_upgrade(
            staging_root, msgspec.structs.replace(batch, objects=tuple(rows))
        )
    kept_jobs = tuple(job for job in batch.verify_jobs if reset.isdisjoint(job.digests))
    return _save_batch_upgrade(
        staging_root,
        msgspec.structs.replace(batch, objects=tuple(rows), verify_jobs=kept_jobs),
    )


def _drive_batch_watch(
    ctx: typer.Context,
    *,
    target: batch_state.BatchTarget,
    object_ids: tuple[str, ...],
    objects_file: Path | None,
    batch_id: str | None,
    category: int | None,
    allow_missing_metadata: bool,
) -> None:
    cfg = require_config(ctx)
    require_simpleproof(ctx, cfg)
    require_bitcoin(ctx, cfg)
    sp_cfg = cfg.simpleproof
    if sp_cfg is None:
        raise RuntimeError("simpleproof config missing")
    if cfg.bitcoin is None:
        raise RuntimeError("bitcoin config missing")
    store = _stage_store(ctx)
    staging_root = store._root
    root_path = _resolve_root_path(ctx)
    sr, _fs = open_root(appctx.root(ctx))
    selected = list(object_ids)
    if objects_file is not None:
        selected.extend(_read_objects_file(objects_file))
    if batch_id is not None and (selected or category is not None):
        err(
            "--batch is mutually exclusive with --object, --objects-file, "
            "and --category"
        )
        raise typer.Exit(2)
    if batch_id is None and not selected:
        err("new batch requires --object or --objects-file")
        raise typer.Exit(2)

    if batch_id is None:
        now = now_utc_iso()
        batch = batch_state.BatchFile(
            batch_id=batch_state.new_batch_id(),
            target=target,
            root_path=root_path,
            category_id=category if category is not None else sp_cfg.proof_category_id,
            created_at=now,
            updated_at=now,
        )
        created = True
    else:
        batch = _load_batch_or_usage(ctx, staging_root, batch_id)
        if batch.target != target:
            err(f"batch {batch.batch_id} target is {batch.target!r}, not {target!r}")
            raise typer.Exit(2)
        created = False

    stream = None
    exit_code = 1
    try:
        with batch_state.with_driver_lease(
            staging_root,
            batch.batch_id,
            command=(*_batch_command_tuple(target, batch.batch_id), "--watch"),
        ):
            if created:
                with batch_state.with_batch_lock(staging_root, batch.batch_id):
                    batch_state.save_batch(staging_root, batch)
                _emit_batch_created(ctx, batch)
                for object_id in dict.fromkeys(selected):
                    row = _prepare_batch_object(
                        target=target,
                        cfg=cfg,
                        store=store,
                        sr=sr,
                        root_path=root_path,
                        staging_root=staging_root,
                        batch=batch,
                        object_id=object_id,
                        allow_missing_metadata=allow_missing_metadata,
                    )
                    batch = _replace_batch_object(batch, row)
                    with batch_state.with_batch_lock(staging_root, batch.batch_id):
                        batch_state.save_batch(staging_root, batch)
                    _emit_batch_object_event(
                        ctx, staging_root=staging_root, batch=batch, row=row
                    )
            else:
                _emit_batch_resumed(ctx, batch)
            recovered = _collect_recoverable_object_intents(store, batch)
            with batch_state.with_batch_lock(staging_root, batch.batch_id):
                before_recovered = batch
                batch = batch_state.load_batch(staging_root, batch.batch_id)
                batch = _merge_recovered_object_intents(batch, recovered)
                batch_state.save_batch(staging_root, batch)
            _emit_batch_object_changes(
                ctx, staging_root=staging_root, before=before_recovered, after=batch
            )
            before_submit = batch
            batch = _submit_batch(
                target=target,
                cfg=cfg,
                store=store,
                staging_root=staging_root,
                batch=batch,
                sp_factory=appctx.sp_client_factory(ctx),
            )
            _emit_batch_object_changes(
                ctx, staging_root=staging_root, before=before_submit, after=batch
            )
            before_upgrade = batch
            batch = _run_batch_upgrade_pass(
                target=target,
                cfg=cfg,
                store=store,
                sr=sr,
                staging_root=staging_root,
                batch=batch,
                sp_factory=appctx.sp_client_factory(ctx),
                btc_factory=appctx.btc_rpc_factory(ctx),
            )
            _emit_batch_object_changes(
                ctx, staging_root=staging_root, before=before_upgrade, after=batch
            )
            if not _batch_has_submitted_subset(batch):
                exit_code = 0 if _batch_watch_successful(batch) else 1
                if exit_code:
                    raise typer.Exit(exit_code)
                return

            btc = appctx.btc_rpc_factory(ctx)(cfg)
            stream = watch._get_zmq_subscriber(cfg)
            stream_cm = cast(
                "contextlib.AbstractContextManager[HashBlockStream]",
                stream
                if hasattr(stream, "__enter__")
                else contextlib.nullcontext(stream),
            )
            last_height: int | None = None
            last_tip_hash: str | None = None
            with stream_cm as zmq_sub:
                while True:
                    block = zmq_sub.wait_for_block(timeout_ms=5000)
                    if block is None:
                        continue
                    watch._sleep_after_block()
                    info = btc.getblockchaininfo()
                    _height_jumped = (
                        last_height is not None and info.height - last_height > 1
                    )
                    _tip_changed_at_stable = (
                        last_height is not None
                        and info.height == last_height
                        and last_tip_hash is not None
                        and last_tip_hash != info.best_block_hash
                    )
                    last_height = info.height
                    last_tip_hash = info.best_block_hash
                    batch = batch_state.load_batch(staging_root, batch.batch_id)
                    before_reverify = batch
                    if _height_jumped or _tip_changed_at_stable:
                        batch = _reverify_batch_watch_rows(
                            target=target,
                            cfg=cfg,
                            store=store,
                            staging_root=staging_root,
                            batch=batch,
                            btc=btc,
                        )
                        _emit_batch_object_changes(
                            ctx,
                            staging_root=staging_root,
                            before=before_reverify,
                            after=batch,
                        )
                    before_upgrade = batch
                    batch = _run_batch_upgrade_pass(
                        target=target,
                        cfg=cfg,
                        store=store,
                        sr=sr,
                        staging_root=staging_root,
                        batch=batch,
                        sp_factory=appctx.sp_client_factory(ctx),
                        btc_factory=appctx.btc_rpc_factory(ctx),
                    )
                    _emit_batch_object_changes(
                        ctx,
                        staging_root=staging_root,
                        before=before_upgrade,
                        after=batch,
                    )
                    if not _batch_has_submitted_subset(batch):
                        exit_code = 0 if _batch_watch_successful(batch) else 1
                        if exit_code:
                            raise typer.Exit(exit_code)
                        return
    except KeyboardInterrupt:
        exit_code = 130
    except (BitcoinError, ReorgDetectedError) as e:
        exit_for_error(e, message=f"halt: {e}")
    finally:
        if appctx.json_mode(ctx):
            _emit_batch_summary(ctx, batch)
        elif batch_id is not None:
            counts = _batch_counts(batch)
            typer.echo(
                f"batch watched: {batch.batch_id} submitted={counts['submitted']} "
                f"upgraded={counts['upgraded']} failed={counts['failed']}"
            )
    if exit_code:
        raise typer.Exit(exit_code)


def _emit_batch_created(ctx: typer.Context, batch: batch_state.BatchFile) -> None:
    data = {
        "event": "batch_created",
        "batch_id": batch.batch_id,
        "target": batch.target,
        "category_id": batch.category_id,
        "root_path": batch.root_path,
        "created_at": batch.created_at,
    }
    if appctx.json_mode(ctx):
        _emit_batch_event(ctx, data)
    else:
        typer.echo(
            f"batch created: {batch.batch_id} target={batch.target} "
            f"category={batch.category_id}"
        )


def _emit_batch_resumed(ctx: typer.Context, batch: batch_state.BatchFile) -> None:
    if appctx.json_mode(ctx):
        _emit_batch_event(
            ctx,
            {
                "event": "batch_resumed",
                "batch_id": batch.batch_id,
                "target": batch.target,
                "category_id": batch.category_id,
                "root_path": batch.root_path,
            },
        )
    else:
        typer.echo(f"batch resumed: {batch.batch_id} target={batch.target}")


def _drive_batch_submit(
    ctx: typer.Context,
    *,
    target: batch_state.BatchTarget,
    object_ids: tuple[str, ...],
    objects_file: Path | None,
    batch_id: str | None,
    category: int | None,
    allow_missing_metadata: bool,
) -> None:
    cfg = require_config(ctx)
    require_simpleproof(ctx, cfg)
    sp_cfg = cfg.simpleproof
    if sp_cfg is None:
        raise RuntimeError("simpleproof config missing")
    store = _stage_store(ctx)
    staging_root = store._root
    root_path = _resolve_root_path(ctx)
    sr, _fs = open_root(appctx.root(ctx))

    selected = list(object_ids)
    if objects_file is not None:
        selected.extend(_read_objects_file(objects_file))
    if batch_id is not None and (selected or category is not None):
        err(
            "--batch is mutually exclusive with --object, --objects-file, "
            "and --category"
        )
        raise typer.Exit(2)
    if batch_id is None and not selected:
        err("new batch requires --object or --objects-file")
        raise typer.Exit(2)

    if batch_id is None:
        now = now_utc_iso()
        batch = batch_state.BatchFile(
            batch_id=batch_state.new_batch_id(),
            target=target,
            root_path=root_path,
            category_id=category if category is not None else sp_cfg.proof_category_id,
            created_at=now,
            updated_at=now,
        )
        created = True
    else:
        batch = _load_batch_or_usage(ctx, staging_root, batch_id)
        if batch.target != target:
            err(f"batch {batch.batch_id} target is {batch.target!r}, not {target!r}")
            raise typer.Exit(2)
        created = False

    with batch_state.with_driver_lease(
        staging_root,
        batch.batch_id,
        command=_batch_command_tuple(target, batch.batch_id),
    ):
        if created:
            with batch_state.with_batch_lock(staging_root, batch.batch_id):
                batch_state.save_batch(staging_root, batch)
            _emit_batch_created(ctx, batch)
            for object_id in dict.fromkeys(selected):
                row = _prepare_batch_object(
                    target=target,
                    cfg=cfg,
                    store=store,
                    sr=sr,
                    root_path=root_path,
                    staging_root=staging_root,
                    batch=batch,
                    object_id=object_id,
                    allow_missing_metadata=allow_missing_metadata,
                )
                batch = _replace_batch_object(batch, row)
                with batch_state.with_batch_lock(staging_root, batch.batch_id):
                    batch_state.save_batch(staging_root, batch)
                _emit_batch_object_event(
                    ctx, staging_root=staging_root, batch=batch, row=row
                )
        else:
            _emit_batch_resumed(ctx, batch)
        recovered = _collect_recoverable_object_intents(store, batch)
        with batch_state.with_batch_lock(staging_root, batch.batch_id):
            before_recovered = batch
            batch = batch_state.load_batch(staging_root, batch.batch_id)
            batch = _merge_recovered_object_intents(batch, recovered)
            batch_state.save_batch(staging_root, batch)
        _emit_batch_object_changes(
            ctx, staging_root=staging_root, before=before_recovered, after=batch
        )
        before_submit = batch
        batch = _submit_batch(
            target=target,
            cfg=cfg,
            store=store,
            staging_root=staging_root,
            batch=batch,
            sp_factory=appctx.sp_client_factory(ctx),
        )
        _emit_batch_object_changes(
            ctx, staging_root=staging_root, before=before_submit, after=batch
        )
    _emit_batch_summary(ctx, batch)
    if not appctx.json_mode(ctx):
        counts = _batch_counts(batch)
        typer.echo(
            f"batch submitted: {batch.batch_id} prepared={counts['prepared']} "
            f"submitted={counts['submitted']} skipped={counts['skipped']} "
            f"failed={counts['failed']}"
        )


@batch_app.command("content")
def timestamp_batch_content(
    ctx: typer.Context,
    object_ids: Annotated[
        list[str] | None,
        typer.Option("--object", help="Object ID to include in a new batch."),
    ] = None,
    objects_file: Annotated[
        Path | None,
        typer.Option("--objects-file", help="File containing object IDs."),
    ] = None,
    batch_id: Annotated[
        str | None,
        typer.Option("--batch", help="Resume an existing batch."),
    ] = None,
    category: Annotated[
        int | None,
        typer.Option("--category", help="SimpleProof category for a new batch."),
    ] = None,
    upgrade: Annotated[
        bool,
        typer.Option("--upgrade", help="Poll SP verify jobs and ingest proofs."),
    ] = False,
    retry: Annotated[
        bool,
        typer.Option("--retry", help="Repair and resubmit failed batch chunks."),
    ] = False,
    watch_flag: Annotated[
        bool,
        typer.Option(
            "--watch",
            help="ZMQ-driven loop; runs a batch upgrade pass per Bitcoin block.",
        ),
    ] = False,
) -> None:
    """Create or resume a content timestamping batch."""
    if retry and (upgrade or watch_flag):
        err("--retry is mutually exclusive with --upgrade and --watch")
        raise typer.Exit(2)
    if upgrade and watch_flag:
        err("--upgrade is mutually exclusive with --watch")
        raise typer.Exit(2)
    if retry:
        if object_ids or objects_file is not None or category is not None:
            err("--retry is only supported with --batch")
            raise typer.Exit(2)
        _drive_batch_retry(ctx, target="content", batch_id=batch_id)
        return
    if watch_flag:
        _drive_batch_watch(
            ctx,
            target="content",
            object_ids=tuple(object_ids or ()),
            objects_file=objects_file,
            batch_id=batch_id,
            category=category,
            allow_missing_metadata=False,
        )
        return
    if upgrade:
        if object_ids or objects_file is not None or category is not None:
            err("--upgrade is only supported with --batch")
            raise typer.Exit(2)
        _drive_batch_upgrade(ctx, target="content", batch_id=batch_id)
        return
    _drive_batch_submit(
        ctx,
        target="content",
        object_ids=tuple(object_ids or ()),
        objects_file=objects_file,
        batch_id=batch_id,
        category=category,
        allow_missing_metadata=False,
    )


@batch_app.command("inventory")
def timestamp_batch_inventory(
    ctx: typer.Context,
    object_ids: Annotated[
        list[str] | None,
        typer.Option("--object", help="Object ID to include in a new batch."),
    ] = None,
    objects_file: Annotated[
        Path | None,
        typer.Option("--objects-file", help="File containing object IDs."),
    ] = None,
    batch_id: Annotated[
        str | None,
        typer.Option("--batch", help="Resume an existing batch."),
    ] = None,
    category: Annotated[
        int | None,
        typer.Option("--category", help="SimpleProof category for a new batch."),
    ] = None,
    allow_missing_metadata: Annotated[
        bool,
        typer.Option(
            "--allow-missing-metadata",
            help="Timestamp inventory even when staged metadata is incomplete.",
        ),
    ] = False,
    upgrade: Annotated[
        bool,
        typer.Option("--upgrade", help="Poll SP verify jobs and ingest proofs."),
    ] = False,
    retry: Annotated[
        bool,
        typer.Option("--retry", help="Repair and resubmit failed batch chunks."),
    ] = False,
    watch_flag: Annotated[
        bool,
        typer.Option(
            "--watch",
            help="ZMQ-driven loop; runs a batch upgrade pass per Bitcoin block.",
        ),
    ] = False,
) -> None:
    """Create or resume an inventory timestamping batch."""
    if retry and (upgrade or watch_flag):
        err("--retry is mutually exclusive with --upgrade and --watch")
        raise typer.Exit(2)
    if upgrade and watch_flag:
        err("--upgrade is mutually exclusive with --watch")
        raise typer.Exit(2)
    if retry:
        if (
            object_ids
            or objects_file is not None
            or category is not None
            or allow_missing_metadata
        ):
            err("--retry is only supported with --batch")
            raise typer.Exit(2)
        _drive_batch_retry(ctx, target="inventory", batch_id=batch_id)
        return
    if watch_flag:
        _drive_batch_watch(
            ctx,
            target="inventory",
            object_ids=tuple(object_ids or ()),
            objects_file=objects_file,
            batch_id=batch_id,
            category=category,
            allow_missing_metadata=allow_missing_metadata,
        )
        return
    if upgrade:
        if object_ids or objects_file is not None or category is not None:
            err("--upgrade is only supported with --batch")
            raise typer.Exit(2)
        _drive_batch_upgrade(ctx, target="inventory", batch_id=batch_id)
        return
    _drive_batch_submit(
        ctx,
        target="inventory",
        object_ids=tuple(object_ids or ()),
        objects_file=objects_file,
        batch_id=batch_id,
        category=category,
        allow_missing_metadata=allow_missing_metadata,
    )


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
