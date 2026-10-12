"""stage status."""

from typing import TYPE_CHECKING, Annotated

import typer

from ocfl_cli import _appctx as appctx
from ocfl_cli import errors as errors
from ocfl_cli._common import output_json
from ocfl_cli.stage._app import require_existing_staging
from ocfl_cli.stage.render import (
    render_metadata_status,
    render_section,
    render_status_diff,
)
from ocfl_cli.workflow import staging as wf_staging
from ocfl_cli.workflow import status as wf_status

if TYPE_CHECKING:
    from rich.console import Console


def _render_status_ots(out: Console, data: dict) -> None:
    render_section(out, "Object")
    if data["changes"]:
        render_status_diff(out, {"changes": data["changes"]})
    else:
        typer.echo("  no staged changes")

    render_metadata_status(out, data["metadata"], leading_blank=True)

    render_section(out, "Workflow", leading_blank=True)
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
                f"submitted {wf_status.humanize_age(o['age_seconds'])}"
            )
    typer.echo(f"  Next: {data['next_action']}")

    render_section(out, "Network", leading_blank=True)
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


def status(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
) -> None:
    """Show changes in staged version vs committed head (OTS-aware)."""
    out = appctx.console(ctx)
    resolved = require_existing_staging(ctx, object_id)
    store = resolved.store
    root_path = resolved.root_path
    staging_dir = resolved.staging_dir

    json_mode = appctx.json_mode(ctx)
    if wf_staging.is_ots_staging(staging_dir):
        cfg = appctx.config(ctx)
        data = wf_status.status_ots_dict(
            cfg,
            store,
            root_path,
            object_id,
            staging_dir,
            btc_factory=appctx.btc_rpc_factory(ctx),
            runtime=appctx.runtime(ctx),
        )
        if json_mode:
            output_json(data)
        else:
            _render_status_ots(out, data)
    else:
        data = wf_status.status_diff_dict(store, root_path, object_id)
        if json_mode:
            output_json(data)
        else:
            render_status_diff(out, data)
            render_metadata_status(
                out, data["metadata"], leading_blank=bool(data["changes"])
            )
