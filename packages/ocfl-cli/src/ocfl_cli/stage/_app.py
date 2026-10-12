"""The stage command group: Typer apps, the group callback, shared preflight."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from ocfl_cli import _appctx as appctx
from ocfl_cli.errors import ConfigError, UsageError
from ocfl_cli.workflow import staging as wf_staging

if TYPE_CHECKING:
    from ocfl_cli.config import Config, SimpleProofConfig
    from ocfl_cli.ports import BtcRpcFactory, SpClientFactory
    from ocfl_cli.workflow.staging import StagingResolved


OTS_HELP_PANEL = "OTS"


stage_app = typer.Typer(
    name="stage",
    help="Stage changes for OCFL objects.",
    no_args_is_help=True,
)

timestamp_app = typer.Typer(
    name="timestamp",
    help="Timestamp staged content / inventory via SimpleProof.",
    no_args_is_help=True,
)

batch_app = typer.Typer(
    name="batch",
    help="Inspect and clean up batch timestamping state.",
    no_args_is_help=True,
)


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


def resolve_root_path(ctx: typer.Context) -> str:
    """Resolve absolute root path string for staging path hashing.

    S3 URIs are returned as-is: ``Path.resolve`` would prefix the cwd.
    """
    root = appctx.root(ctx)
    if root is not None and root.startswith("s3://"):
        return root
    return str(Path(root or Path.cwd()).resolve())


def stage_store(ctx: typer.Context):
    """The Staging Area store the group callback opened for this run."""
    obj = ctx.obj
    if not isinstance(obj, dict):
        raise RuntimeError("internal error: expected ctx.obj to be a dict")
    return wf_staging.require_store(obj.get("staging"))


def require_config(ctx: typer.Context) -> Config:
    return appctx.config(ctx)


def require_simpleproof(ctx: typer.Context, cfg: Config) -> None:
    if cfg.simpleproof is None:
        raise ConfigError(
            f"{appctx.command_path(ctx)!r} requires [simpleproof] config section"
        )


def require_bitcoin(ctx: typer.Context, cfg: Config) -> None:
    if cfg.bitcoin is None:
        raise ConfigError(
            f"{appctx.command_path(ctx)!r} requires [bitcoin] config section"
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
                raise UsageError(
                    f"'{command} --retry' is incompatible with {other_name}"
                )
    if watch and not upgrade:
        raise UsageError(f"'{command} --watch' requires --upgrade")


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
        raise UsageError(
            f"'{command} --allow-missing-metadata' is only valid when submitting "
            "an inventory timestamp"
        )


def require_existing_staging(
    ctx: typer.Context, object_id: str, *, hint: str | None = None
) -> StagingResolved:
    return wf_staging.resolve_staging(
        stage_store(ctx), resolve_root_path(ctx), object_id, hint=hint
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
    wf_staging.require_ots_staging(resolved.staging_dir, object_id)
    sp_factory, btc_factory = get_factories(ctx)
    return TimestampPreflight(
        cfg=cfg,
        resolved=resolved,
        sp_factory=sp_factory,
        btc_factory=btc_factory,
    )


def require_ots_config(
    cfg: Config | None, digest: DigestOption | None
) -> SimpleProofConfig:
    """Preflight `stage new --ots`. Returns validated [simpleproof] config."""
    if digest is not None and digest is not DigestOption.SHA256:
        raise UsageError(
            f"--ots requires SHA-256 (SimpleProof API constraint); "
            f"got --digest={digest.value!r}"
        )
    where = (
        f"in {cfg.path}"
        if cfg is not None and cfg.path is not None
        else "in your ocfl config"
    )
    if cfg is None or cfg.simpleproof is None:
        raise ConfigError(f"--ots requires [simpleproof] section {where}")
    if cfg.bitcoin is None:
        raise ConfigError(f"--ots requires [bitcoin] section {where}")
    return cfg.simpleproof


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
    root = Path(staging_root) if staging_root else wf_staging.default_staging_root()
    ctx.obj["staging"] = wf_staging.open_store(root)
