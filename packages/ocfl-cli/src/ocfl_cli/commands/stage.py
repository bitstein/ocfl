"""ocfl stage command group."""

import os
from pathlib import Path
from typing import Annotated

import typer

from ocfl_cli.staging import StagingStore

stage_app = typer.Typer(
    name="stage",
    help="Stage changes for OCFL objects.",
    no_args_is_help=True,
)

_DEFAULT_STAGING_ROOT = (
    Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
    / "ocfl-cli"
    / "staging"
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
    root = Path(staging_root) if staging_root else _DEFAULT_STAGING_ROOT
    root.mkdir(parents=True, exist_ok=True)
    ctx.obj["staging"] = StagingStore(root)
