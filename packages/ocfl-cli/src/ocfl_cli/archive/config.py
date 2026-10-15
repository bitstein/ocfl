import os
from pathlib import Path
from typing import Any

import typer

from ocfl_cli import _appctx as appctx
from ocfl_cli._appctx import AppCtx, BrokenConfig
from ocfl_cli._common import output_json
from ocfl_cli.config import config_path, env_file_path, redacted, user_env_file_path
from ocfl_cli.workflow import config_check
from ocfl_cli.workflow import staging as wf_staging

config_app = typer.Typer(
    name="config",
    help="Show where the config is, what it says, and whether it works.",
    no_args_is_help=True,
)


def _staging_root() -> Path:
    override = os.environ.get("OCFL_STAGING_ROOT")
    return Path(override) if override else wf_staging.default_staging_root()


@config_app.command("path")
def path_cmd(ctx: typer.Context) -> None:
    """Print the config file, secrets file and staging root paths."""
    state = appctx.tolerating_broken_config(ctx)
    cfg_path = config_path()
    env_path = env_file_path() or user_env_file_path()
    data = {
        "config": str(cfg_path),
        "config_exists": cfg_path.is_file(),
        "env_file": str(env_path),
        "env_file_exists": env_path.is_file(),
        "staging_root": str(_staging_root()),
    }
    if state.json:
        output_json(data)
        return
    for key in ("config", "env_file", "staging_root"):
        missing = key != "staging_root" and not data[f"{key}_exists"]
        typer.echo(f"{key}: {data[key]}{' (missing)' if missing else ''}")


def _lines(prefix: str, value: Any) -> list[str]:
    if isinstance(value, dict):
        return [ln for k, v in value.items() for ln in _lines(f"{prefix}.{k}", v)]
    return [f"{prefix.lstrip('.')} = {value}"]


@config_app.command("show")
def show_cmd(ctx: typer.Context) -> None:
    """Print the effective config; secrets are shown as <redacted>."""
    app = appctx.of(ctx)
    data = redacted(app.config)
    if app.json:
        output_json(data)
        return
    for section, value in data.items():
        if value is None or value == {}:
            continue
        for line in _lines(section, value):
            typer.echo(line)


@config_app.command("check")
def check_cmd(ctx: typer.Context) -> None:
    """Check the config, the secrets file, SimpleProof, Bitcoin RPC and ZMQ.

    Exits 6 when the config or a credential is wrong, 1 when a service
    cannot be reached.
    """
    state = appctx.tolerating_broken_config(ctx)
    match state:
        case BrokenConfig(error=error):
            checks = config_check.broken_config_checks(error)
        case AppCtx() as app:
            checks = config_check.run_checks(
                app.config,
                sp_factory=app.sp_client_factory,
                btc_factory=app.btc_rpc_factory,
            )
    if state.json:
        output_json(
            {"ok": all(c.ok for c in checks), "checks": [c.as_dict() for c in checks]}
        )
    else:
        for c in checks:
            typer.echo(f"{'ok  ' if c.ok else 'FAIL'}  {c.name}: {c.detail}")
    config_check.require_all_ok(checks)
