"""OCFL CLI entry point."""

import os
from typing import Annotated

import typer

from ocfl_cli import _style
from ocfl_cli.commands.diff import diff
from ocfl_cli.commands.export import export
from ocfl_cli.commands.info import info
from ocfl_cli.commands.init import init
from ocfl_cli.commands.log import log
from ocfl_cli.commands.ls import ls
from ocfl_cli.commands.purge import purge
from ocfl_cli.commands.show import show
from ocfl_cli.commands.stage import stage_app
from ocfl_cli.commands.validate import validate
from ocfl_cli.config import ConfigError, load_config

app = typer.Typer(
    name="ocfl",
    help="CLI for managing an OCFL archive.",
    no_args_is_help=True,
)

app.command()(diff)
app.command()(export)
app.command()(info)
app.command()(init)
app.command()(log)
app.command()(ls)
app.command()(purge)
app.command()(show)
app.command()(validate)
app.add_typer(stage_app)


@app.callback()
def main(
    ctx: typer.Context,
    root: Annotated[
        str | None,
        typer.Option(
            "--root", "-r", envvar="OCFL_ROOT", help="Path to OCFL storage root."
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON."),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Suppress non-essential output."),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show verbose output."),
    ] = False,
    no_color: Annotated[
        bool,
        typer.Option("--no-color", help="Disable colored output."),
    ] = False,
) -> None:
    """CLI for managing an OCFL archive."""
    if no_color:
        _style.set_no_color()
    ctx.ensure_object(dict)

    try:
        config = load_config()
    except ConfigError as e:
        from ocfl_cli._common import err

        err(str(e))
        raise typer.Exit(1) from None

    # Resolve `-r` through config: alias → archive root. Precedence:
    # flag > OCFL_ROOT env > implicit-single-archive default.
    archive = None
    resolved_root = root
    if root is not None:
        a = config.archive(root)
        if a is not None:
            archive = a
            resolved_root = a.root
    elif os.environ.get("OCFL_ROOT") is None:
        implicit = config.implicit_default()
        if implicit is not None:
            archive = implicit
            resolved_root = implicit.root

    ctx.obj["root"] = resolved_root
    ctx.obj["config"] = config
    ctx.obj["archive"] = archive
    ctx.obj["json"] = json_output
    ctx.obj["quiet"] = quiet
    ctx.obj["verbose"] = verbose
    ctx.obj["no_color"] = no_color
