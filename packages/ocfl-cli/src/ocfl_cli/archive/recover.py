from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from ocfl_cli import _appctx as appctx
from ocfl_cli._common import open_root, output_json, resolve_root
from ocfl_cli.errors import UsageError
from ocfl_cli.workflow.commit import staged_content_for_recovery
from ocfl_cli.workflow.staging import default_staging_root
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
    app = appctx.of(ctx)
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

    root, fs = open_root(app)
    content = (
        _staged_content(app.root, staging_root, object_id)
        if action is RecoveryAction.RESUME and not isinstance(fs, ClaimFS)
        else None
    )
    status = root.recover(fs, object_id, action, version=remove_stray, content=content)

    if app.json:
        output_json(
            {
                "object_id": object_id,
                "state": status.state.value,
                "action": action.value,
                "version": status.version,
            }
        )
    elif status.state is CommitState.CLEAN:
        app.console.print(f"{object_id}: no unfinished commit")
    else:
        app.console.print(
            f"{object_id}: {status.state.value} {status.version}: "
            f"{action.value.replace('_', '-')} done"
        )


def _staged_content(
    root: str | None, staging_root: str | None, object_id: str
) -> ContentSource | None:
    path = Path(staging_root) if staging_root else default_staging_root()
    return staged_content_for_recovery(path, resolve_root(root), object_id)
