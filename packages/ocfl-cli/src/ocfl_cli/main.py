"""OCFL CLI entry point."""

import json
import logging
import os
import sys
from typing import Annotated

import typer

from ocfl_cli._appctx import install_app_ctx
from ocfl_cli._common import DEFAULT_FS_FACTORY
from ocfl_cli._exit import OcflGroup
from ocfl_cli.archive.config import config_app
from ocfl_cli.archive.diff import diff
from ocfl_cli.archive.export import export
from ocfl_cli.archive.info import info
from ocfl_cli.archive.init import init
from ocfl_cli.archive.log import log
from ocfl_cli.archive.ls import ls
from ocfl_cli.archive.purge import purge
from ocfl_cli.archive.recover import recover
from ocfl_cli.archive.show import show
from ocfl_cli.archive.validate import validate
from ocfl_cli.config import Config, load_config
from ocfl_cli.errors import ConfigError
from ocfl_cli.exit_codes import format_exit_code_help
from ocfl_cli.ots.bitcoin import default_block_stream_factory, default_btc_factory
from ocfl_cli.ots.simpleproof import default_sp_factory
from ocfl_cli.stage import stage_app
from ocfl_cli.system import SYSTEM_CLOCK, SYSTEM_SLEEPER

ARCHIVE_PANEL = "Archive"
STAGING_PANEL = "Staging"
STORAGE_ADMIN_PANEL = "Storage admin"


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


def configure_logging(json_mode: bool, verbose: bool, quiet: bool) -> None:
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
        root.setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.INFO if verbose else logging.WARNING)


app = typer.Typer(
    cls=OcflGroup,
    name="ocfl",
    help="CLI for managing an OCFL archive.",
    # Rich help joins single newlines in an epilog; double ones break the line.
    epilog=format_exit_code_help().replace("\n", "\n\n"),
    no_args_is_help=True,
)

app.command(rich_help_panel=ARCHIVE_PANEL)(diff)
app.command(rich_help_panel=ARCHIVE_PANEL)(export)
app.command(rich_help_panel=ARCHIVE_PANEL)(info)
app.command(rich_help_panel=STORAGE_ADMIN_PANEL)(init)
app.command(rich_help_panel=ARCHIVE_PANEL)(log)
app.command(rich_help_panel=ARCHIVE_PANEL)(ls)
app.command(rich_help_panel=STORAGE_ADMIN_PANEL)(purge)
app.command(rich_help_panel=STORAGE_ADMIN_PANEL)(recover)
app.command(rich_help_panel=ARCHIVE_PANEL)(show)
app.command(rich_help_panel=STORAGE_ADMIN_PANEL)(validate)
app.add_typer(config_app, rich_help_panel=STORAGE_ADMIN_PANEL)
app.add_typer(stage_app, rich_help_panel=STAGING_PANEL)


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
    configure_logging(
        json_mode=log_json or os.environ.get("OCFL_LOG_JSON") == "1",
        verbose=verbose,
        quiet=quiet,
    )
    ctx.ensure_object(dict)

    try:
        config = load_config()
    except ConfigError as e:
        # `config path|check` diagnose a broken config instead of failing on it.
        if ctx.invoked_subcommand != "config":
            raise
        config = Config()
        ctx.obj["config_error"] = e

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
        clock=SYSTEM_CLOCK,
        sleeper=SYSTEM_SLEEPER,
        block_stream_factory=default_block_stream_factory,
        fs_factory=DEFAULT_FS_FACTORY,
    )
