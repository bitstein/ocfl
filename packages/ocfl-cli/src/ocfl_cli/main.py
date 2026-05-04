"""OCFL CLI entry point."""

import json
import logging
import os
import sys
from typing import Annotated

import typer

from ocfl_cli import _style
from ocfl_cli._appctx import install_app_ctx
from ocfl_cli._exit import exit_for_error
from ocfl_cli.archive.diff import diff
from ocfl_cli.archive.export import export
from ocfl_cli.archive.info import info
from ocfl_cli.archive.init import init
from ocfl_cli.archive.log import log
from ocfl_cli.archive.ls import ls
from ocfl_cli.archive.purge import purge
from ocfl_cli.archive.show import show
from ocfl_cli.archive.validate import validate
from ocfl_cli.config import ConfigError, load_config
from ocfl_cli.ots.bitcoin import default_btc_factory
from ocfl_cli.ots.simpleproof import default_sp_factory
from ocfl_cli.stage import stage_app


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def _configure_logging(json_mode: bool, verbose: bool, quiet: bool) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_ocfl_handler", False):
            root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler._ocfl_handler = True  # type: ignore[attr-defined]
    if json_mode:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    if quiet:
        root.setLevel(logging.ERROR)
    elif verbose:
        root.setLevel(logging.DEBUG)
    else:
        root.setLevel(logging.INFO)


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
    log_json: Annotated[
        bool,
        typer.Option("--log-json", help="Emit structured JSON log lines."),
    ] = False,
) -> None:
    """CLI for managing an OCFL archive."""
    if no_color:
        _style.set_no_color()
    _configure_logging(
        json_mode=log_json or os.environ.get("OCFL_LOG_JSON") == "1",
        verbose=verbose,
        quiet=quiet,
    )
    ctx.ensure_object(dict)

    try:
        config = load_config()
    except ConfigError as e:
        exit_for_error(e)

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

    install_app_ctx(
        ctx,
        root=resolved_root,
        config=config,
        archive=archive,
        json_output=json_output,
        quiet=quiet,
        verbose=verbose,
        no_color=no_color,
        sp_client_factory=default_sp_factory,
        btc_rpc_factory=default_btc_factory,
    )
