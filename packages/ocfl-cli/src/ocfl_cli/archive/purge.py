"""ocfl purge command."""

from typing import Annotated

import typer

from ocfl_cli import _appctx as appctx
from ocfl_cli._common import open_root


def purge(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID to purge.")],
    force: Annotated[
        bool,
        typer.Option("--force", help="Skip confirmation prompt."),
    ] = False,
) -> None:
    """Permanently remove an object from the storage root."""
    root, fs = open_root(appctx.root(ctx))

    if not force:
        typer.confirm(
            f"Permanently delete {object_id!r}? This cannot be undone",
            abort=True,
        )

    root.purge(fs, object_id)
