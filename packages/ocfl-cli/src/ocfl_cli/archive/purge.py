"""ocfl purge command."""

from typing import Annotated

import typer

from ocfl_cli._common import open_root


def purge(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID to purge.")],
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the confirmation prompt."),
    ] = False,
    force: Annotated[
        bool,
        # The old spelling of --yes. Elsewhere --force overrides a safety
        # check, so it is hidden here.
        typer.Option("--force", hidden=True),
    ] = False,
) -> None:
    """Permanently remove an object from the storage root."""
    root, fs = open_root(ctx)

    if not (yes or force):
        typer.confirm(
            f"Permanently delete {object_id!r}? This cannot be undone",
            abort=True,
        )
    root.purge(fs, object_id)
