"""The `stage` command group: commands only; the work is in `ocfl_cli.workflow`."""

from ocfl_cli.stage._app import OTS_HELP_PANEL, batch_app, stage_app, timestamp_app
from ocfl_cli.stage.batch import (
    timestamp_batch_content,
    timestamp_batch_inventory,
    timestamp_batch_ls,
    timestamp_batch_rm,
    timestamp_batch_status,
)
from ocfl_cli.stage.commit import commit
from ocfl_cli.stage.edit import add, ls_cmd, metadata, mv, new, reset, rm
from ocfl_cli.stage.status import status
from ocfl_cli.stage.timestamp import timestamp_content, timestamp_inventory

# Registration order is the order `--help` lists the commands in.
stage_app.command()(new)
stage_app.command()(metadata)
stage_app.command()(add)
stage_app.command()(mv)
stage_app.command()(rm)
stage_app.command("ls")(ls_cmd)
stage_app.command()(status)
stage_app.command()(commit)
stage_app.add_typer(timestamp_app, rich_help_panel=OTS_HELP_PANEL)
timestamp_app.add_typer(batch_app)
batch_app.command("content")(timestamp_batch_content)
batch_app.command("inventory")(timestamp_batch_inventory)
batch_app.command("ls")(timestamp_batch_ls)
batch_app.command("status")(timestamp_batch_status)
batch_app.command("rm")(timestamp_batch_rm)
timestamp_app.command("content")(timestamp_content)
timestamp_app.command("inventory")(timestamp_inventory)
stage_app.command()(reset)

__all__ = ["stage_app"]
