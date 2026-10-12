"""ocfl recover command (ADR-0001)."""

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from platformdirs import user_state_path

from ocfl_cli import _appctx as appctx
from ocfl_cli import _style
from ocfl_cli._common import open_root, output_json, resolve_root
from ocfl_cli.errors import UsageError
from ocfl_lib import ClaimFS, CommitState, RecoveryAction

if TYPE_CHECKING:
    from ocfl_lib import ContentSource


def recover(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID to recover.")],
    resume: Annotated[
        bool,
        typer.Option("--resume", help="Finish the unfinished commit."),
    ] = False,
    revert: Annotated[
        bool,
        typer.Option("--revert", help="Undo the unfinished commit."),
    ] = False,
    remove_stray: Annotated[
        str | None,
        typer.Option(
            "--remove-stray",
            metavar="VN",
            help="Remove the stray claim VN (a version with no plan).",
        ),
    ] = None,
    staging_root: Annotated[
        str | None,
        typer.Option(
            "--staging-root",
            envvar="OCFL_STAGING_ROOT",
            help="Staging directory with the object's content (S3 --resume).",
        ),
    ] = None,
) -> None:
    """Finish or undo an object's interrupted commit, read from storage."""
    if resume + revert + (remove_stray is not None) != 1:
        raise UsageError(
            "exactly one of --resume, --revert or --remove-stray VN is required"
        )
    if resume:
        action = RecoveryAction.RESUME
    elif revert:
        action = RecoveryAction.REVERT
    else:
        action = RecoveryAction.REMOVE_STRAY

    root, fs = open_root(appctx.root(ctx))
    content = (
        _staged_content(ctx, staging_root, object_id)
        if action is RecoveryAction.RESUME and not isinstance(fs, ClaimFS)
        else None
    )
    status = root.recover(fs, object_id, action, version=remove_stray, content=content)

    if appctx.json_mode(ctx):
        output_json(
            {
                "object_id": object_id,
                "state": status.state.value,
                "action": action.value,
                "version": status.version,
            }
        )
    elif status.state is CommitState.CLEAN:
        _style.echo(f"{object_id}: no unfinished commit")
    else:
        _style.echo(
            f"{object_id}: {status.state.value} {status.version}: "
            f"{action.value.replace('_', '-')} done"
        )


def _staged_content(
    ctx: typer.Context, staging_root: str | None, object_id: str
) -> ContentSource | None:
    """The object's staged content, if it is still staged."""
    from ocfl_cli.stage import service
    from ocfl_cli.stage.store import StagingStore

    path = (
        Path(staging_root) if staging_root else user_state_path("ocfl-cli") / "staging"
    )
    if not path.is_dir():
        return None
    store = StagingStore(path)
    root_path = resolve_root(appctx.root(ctx))
    if not store.exists(root_path, object_id):
        return None
    return service.staged_content_source(store, root_path, object_id)
