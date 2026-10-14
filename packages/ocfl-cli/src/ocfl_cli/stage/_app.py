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

batch_app = typer.Typer(
    name="batch",
    help="Timestamp many Staging Areas together: create, drive, commit, clean up.",
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


def resolve_root_path(ctx: typer.Context) -> str:
    root = appctx.root(ctx)
    if root is not None and root.startswith("s3://"):
        return root
    return str(Path(root or Path.cwd()).resolve())


def stage_store(ctx: typer.Context):
    obj = ctx.obj
    if not isinstance(obj, dict):
        raise RuntimeError("internal error: expected ctx.obj to be a dict")
    return wf_staging.require_store(obj.get("staging"))


def require_config(ctx: typer.Context) -> Config:
    return appctx.config(ctx)


def require_simpleproof(ctx: typer.Context, cfg: Config) -> None:
    cfg.require("simpleproof")
    if cfg.simpleproof is None:
        raise ConfigError(
            f"{appctx.command_path(ctx)!r} requires [simpleproof] config section"
        )


def require_bitcoin(ctx: typer.Context, cfg: Config) -> None:
    cfg.require("bitcoin")
    if cfg.bitcoin is None:
        raise ConfigError(
            f"{appctx.command_path(ctx)!r} requires [bitcoin] config section"
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
    ctx: typer.Context, object_id: str, *, bitcoin: bool
) -> TimestampPreflight:
    cfg = require_config(ctx)
    require_simpleproof(ctx, cfg)
    if bitcoin:
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
    if cfg is not None:
        cfg.require("simpleproof")
        cfg.require("bitcoin")
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
