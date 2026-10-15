from dataclasses import dataclass, fields
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from ocfl_cli import _appctx as appctx
from ocfl_cli._appctx import AppCtx
from ocfl_cli.errors import ConfigError, UsageError
from ocfl_cli.workflow import staging as wf_staging

if TYPE_CHECKING:
    from ocfl_cli.config import Config, SimpleProofConfig
    from ocfl_cli.ports import BtcRpcFactory, SpClientFactory
    from ocfl_cli.workflow.staging import StagingResolved, StagingStore


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
class StageCtx(AppCtx):
    store: StagingStore

    @classmethod
    def extend(cls, app: AppCtx, store: StagingStore) -> StageCtx:
        return cls(
            **{f.name: getattr(app, f.name) for f in fields(AppCtx)}, store=store
        )


def stage_of(ctx: typer.Context) -> StageCtx:
    app = appctx.of(ctx)
    if not isinstance(app, StageCtx):
        raise appctx.AppCtxError(
            "internal error: stage command outside the stage group"
        )
    return app


@dataclass(frozen=True, slots=True)
class TimestampPreflight:
    cfg: Config
    resolved: StagingResolved
    sp_factory: SpClientFactory
    btc_factory: BtcRpcFactory


class DigestOption(StrEnum):
    SHA512 = "sha512"
    SHA256 = "sha256"


def resolve_root_path(root: str | None) -> str:
    if root is not None and root.startswith("s3://"):
        return root
    return str(Path(root or Path.cwd()).resolve())


def require_simpleproof(ctx: typer.Context, cfg: Config) -> None:
    cfg.raise_if_broken("simpleproof")
    if cfg.simpleproof is None:
        raise ConfigError(
            f"{appctx.command_path(ctx)!r} requires [simpleproof] config section"
        )


def require_bitcoin(ctx: typer.Context, cfg: Config) -> None:
    cfg.raise_if_broken("bitcoin")
    if cfg.bitcoin is None:
        raise ConfigError(
            f"{appctx.command_path(ctx)!r} requires [bitcoin] config section"
        )


def require_existing_staging(
    stage: StageCtx, object_id: str, *, hint: str | None = None
) -> StagingResolved:
    return wf_staging.resolve_staging(
        stage.store, resolve_root_path(stage.root), object_id, hint=hint
    )


def require_timestamp_preflight(
    ctx: typer.Context, object_id: str, *, bitcoin: bool
) -> TimestampPreflight:
    stage = stage_of(ctx)
    cfg = stage.config
    require_simpleproof(ctx, cfg)
    if bitcoin:
        require_bitcoin(ctx, cfg)

    resolved = require_existing_staging(stage, object_id)
    wf_staging.require_ots_staging(resolved.staging_dir, object_id)
    return TimestampPreflight(
        cfg=cfg,
        resolved=resolved,
        sp_factory=stage.sp_client_factory,
        btc_factory=stage.btc_rpc_factory,
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
        cfg.raise_if_broken("simpleproof")
        cfg.raise_if_broken("bitcoin")
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
    app = appctx.of(ctx)
    root = Path(staging_root) if staging_root else wf_staging.default_staging_root()
    ctx.obj = StageCtx.extend(app, wf_staging.open_store(root))
