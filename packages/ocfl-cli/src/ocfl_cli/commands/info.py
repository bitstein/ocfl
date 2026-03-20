"""ocfl info command."""

from typing import Annotated

import typer

from ocfl_cli._common import err, open_root, output_json


def info(
    ctx: typer.Context,
    object_id: Annotated[
        str | None, typer.Argument(help="Object ID (omit for root info).")
    ] = None,
) -> None:
    """Show metadata for storage root or a specific object."""
    json_mode = ctx.obj.get("json", False)

    if object_id is not None:
        _info_object(ctx, object_id, json_mode)
    else:
        _info_root(ctx, json_mode)


def _info_root(ctx: typer.Context, json_mode: bool) -> None:
    root, _fs = open_root(ctx.obj.get("root"))

    layout_name = root.layout.name if root.layout else None

    if json_mode:
        data: dict[str, str | None] = {
            "spec": str(root.spec),
            "layout": layout_name,
        }
        output_json(data)
    else:
        typer.echo(f"Spec:   {root.spec}")
        typer.echo(f"Layout: {layout_name or '(none)'}")


def _info_object(ctx: typer.Context, object_id: str, json_mode: bool) -> None:
    root, _fs = open_root(ctx.obj.get("root"))

    try:
        obj = root.get_object(object_id)
        if not obj.exists:
            raise ValueError(f"object {object_id!r} not found")
    except ValueError as e:
        err(str(e))
        raise typer.Exit(1) from None

    inv = obj.inventory.inventory
    num_versions = len(inv.versions)

    if json_mode:
        output_json(
            {
                "id": inv.id,
                "spec": str(obj.spec),
                "digest_algorithm": inv.digest_algorithm,
                "head": inv.head,
                "num_versions": num_versions,
            }
        )
    else:
        typer.echo(f"ID:        {inv.id}")
        typer.echo(f"Spec:      {obj.spec}")
        typer.echo(f"Algorithm: {inv.digest_algorithm}")
        typer.echo(f"Head:      {inv.head}")
        typer.echo(f"Versions:  {num_versions}")
