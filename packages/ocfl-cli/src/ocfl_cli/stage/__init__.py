from ocfl_cli.stage._app import OTS_HELP_PANEL, batch_app, stage_app
from ocfl_cli.stage.batch import (
    batch_commit,
    batch_create,
    batch_ls,
    batch_retry,
    batch_rm,
    batch_status,
    batch_submit,
    batch_upgrade,
)
from ocfl_cli.stage.commit import commit
from ocfl_cli.stage.edit import add, discard, ls_cmd, metadata, mv, new, rm
from ocfl_cli.stage.status import status
from ocfl_cli.stage.timestamp import retry_cmd, timestamp_cmd, upgrade_cmd

stage_app.command()(new)
stage_app.command()(metadata)
stage_app.command()(add)
stage_app.command()(mv)
stage_app.command()(rm)
stage_app.command("ls")(ls_cmd)
stage_app.command()(status)
stage_app.command()(commit)
stage_app.command("timestamp", rich_help_panel=OTS_HELP_PANEL)(timestamp_cmd)
stage_app.command("upgrade", rich_help_panel=OTS_HELP_PANEL)(upgrade_cmd)
stage_app.command("retry", rich_help_panel=OTS_HELP_PANEL)(retry_cmd)
stage_app.add_typer(batch_app, rich_help_panel=OTS_HELP_PANEL)
batch_app.command("create")(batch_create)
batch_app.command("submit")(batch_submit)
batch_app.command("upgrade")(batch_upgrade)
batch_app.command("retry")(batch_retry)
batch_app.command("commit")(batch_commit)
batch_app.command("status")(batch_status)
batch_app.command("ls")(batch_ls)
batch_app.command("rm")(batch_rm)
stage_app.command()(discard)

__all__ = ["stage_app"]
