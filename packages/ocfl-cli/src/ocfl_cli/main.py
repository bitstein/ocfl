"""OCFL CLI entry point."""

from typing import Annotated

import typer

from ocfl_cli.commands.diff import diff
from ocfl_cli.commands.export import export
from ocfl_cli.commands.info import info
from ocfl_cli.commands.init import init
from ocfl_cli.commands.log import log
from ocfl_cli.commands.ls import ls
from ocfl_cli.commands.show import show
from ocfl_cli.commands.validate import validate

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
app.command()(show)
app.command()(validate)


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
) -> None:
    """CLI for managing an OCFL archive."""
    ctx.ensure_object(dict)
    ctx.obj["root"] = root
    ctx.obj["json"] = json_output
    ctx.obj["quiet"] = quiet
    ctx.obj["verbose"] = verbose
