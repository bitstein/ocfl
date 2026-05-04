"""ocfl log command."""

from typing import Annotated

import typer
from rich.text import Text

from ocfl_cli import _style
from ocfl_cli._appctx import json_mode
from ocfl_cli._appctx import root as ctx_root
from ocfl_cli._common import err, open_root, output_json
from ocfl_lib import ObjectNotFound, OcflError


def log(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    reverse: Annotated[
        bool, typer.Option("--reverse", help="Show oldest first.")
    ] = False,
    num: Annotated[
        int | None, typer.Option("-n", help="Limit number of versions shown.")
    ] = None,
) -> None:
    """Show version history of an object."""
    as_json = json_mode(ctx)
    root, _fs = open_root(ctx_root(ctx))

    try:
        obj = root.get_object(object_id)
        if not obj.exists:
            raise ObjectNotFound(object_id)
    except OcflError as e:
        err(str(e))
        raise typer.Exit(1) from None

    versions = obj.versions()
    if not reverse:
        versions = list(reversed(versions))
    if num is not None:
        versions = versions[:num]

    if as_json:
        entries = []
        for v in versions:
            info = obj.version_info(v)
            entry: dict[str, str | None] = {
                "version": str(v),
                "created": info.created,
                "message": info.message,
            }
            if info.user:
                entry["user_name"] = info.user.name
                entry["user_address"] = info.user.address
            entries.append(entry)
        output_json(entries)
    else:
        for v in versions:
            info = obj.version_info(v)
            _style.echo(
                Text.assemble(_style.version(str(v)), "  ", _style.date(info.created))
            )
            if info.message:
                typer.echo(f"    {info.message}")
            if info.user:
                user_str = info.user.name
                if info.user.address:
                    user_str += f" <{info.user.address}>"
                typer.echo(f"    {user_str}")
            typer.echo()
