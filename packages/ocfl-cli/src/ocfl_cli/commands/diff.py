"""ocfl diff command."""

from typing import Annotated

import typer

from ocfl_cli._common import err, open_root, output_json, parse_version

_ACTION_SYMBOLS = {"added": "A", "deleted": "D", "modified": "M", "renamed": "R"}


def diff(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    v1: Annotated[str, typer.Argument(help="First version (e.g. v1 or 1).")],
    v2: Annotated[str, typer.Argument(help="Second version (e.g. v2 or 2).")],
) -> None:
    """Show changes between two object versions."""
    json_mode = ctx.obj.get("json", False)
    root, _fs = open_root(ctx.obj.get("root"))

    try:
        obj = root.get_object(object_id)
        if not obj.exists:
            raise ValueError(f"object {object_id!r} not found")
    except ValueError as e:
        err(str(e))
        raise typer.Exit(1) from None

    vnum1 = parse_version(v1)
    vnum2 = parse_version(v2)
    entries = obj.diff_versions(vnum1, vnum2)

    if json_mode:
        output_json(
            [
                {
                    "path": e.path,
                    "action": e.action,
                    "renamed_from": e.renamed_from,
                }
                for e in entries
            ]
        )
    else:
        for e in entries:
            sym = _ACTION_SYMBOLS.get(e.action, "?")
            line = f"{sym}  {e.path}"
            if e.renamed_from:
                line += f"  (from {e.renamed_from})"
            typer.echo(line)
