from typing import Annotated

import typer
from rich.text import Text

from ocfl_cli import _appctx as appctx
from ocfl_cli import _style
from ocfl_cli._common import open_root, output_json
from ocfl_lib import ObjectNotFound


def info(
    ctx: typer.Context,
    object_id: Annotated[
        str | None, typer.Argument(help="Object ID (omit for root info).")
    ] = None,
) -> None:
    """Show metadata for storage root or a specific object."""
    as_json = appctx.json_mode(ctx)

    if object_id is not None:
        _info_object(ctx, object_id, as_json)
    else:
        _info_root(ctx, as_json)


def _info_root(ctx: typer.Context, json_mode: bool) -> None:
    root, _fs = open_root(ctx)

    layout_name = root.layout.name if root.layout else None

    if json_mode:
        data: dict[str, str | None] = {
            "spec": str(root.spec),
            "layout": layout_name,
        }
        output_json(data)
    else:
        appctx.console(ctx).print(Text.assemble(_style.key("Spec:   "), str(root.spec)))
        appctx.console(ctx).print(
            Text.assemble(_style.key("Layout: "), layout_name or "(none)")
        )


def _info_object(ctx: typer.Context, object_id: str, json_mode: bool) -> None:
    root, _fs = open_root(ctx)

    obj = root.get_object(object_id)
    if not obj.exists:
        raise ObjectNotFound(object_id)

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
        appctx.console(ctx).print(Text.assemble(_style.key("ID:        "), inv.id))
        appctx.console(ctx).print(
            Text.assemble(_style.key("Spec:      "), str(obj.spec))
        )
        appctx.console(ctx).print(
            Text.assemble(_style.key("Algorithm: "), inv.digest_algorithm)
        )
        appctx.console(ctx).print(Text.assemble(_style.key("Head:      "), inv.head))
        appctx.console(ctx).print(
            Text.assemble(_style.key("Versions:  "), str(num_versions))
        )
