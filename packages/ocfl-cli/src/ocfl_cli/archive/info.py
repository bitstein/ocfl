from typing import TYPE_CHECKING, Annotated

import typer
from rich.text import Text

from ocfl_cli import _appctx as appctx
from ocfl_cli import _style
from ocfl_cli._common import open_root, output_json
from ocfl_lib import ObjectNotFound

if TYPE_CHECKING:
    from ocfl_cli._appctx import AppCtx


def info(
    ctx: typer.Context,
    object_id: Annotated[
        str | None, typer.Argument(help="Object ID (omit for root info).")
    ] = None,
) -> None:
    """Show metadata for storage root or a specific object."""
    app = appctx.of(ctx)
    if object_id is not None:
        _info_object(app, object_id)
    else:
        _info_root(app)


def _info_root(app: AppCtx) -> None:
    root, _fs = open_root(app)

    layout_name = root.layout.name if root.layout else None

    if app.json:
        data: dict[str, str | None] = {
            "spec": str(root.spec),
            "layout": layout_name,
        }
        output_json(data)
    else:
        app.console.print(Text.assemble(_style.key("Spec:   "), str(root.spec)))
        app.console.print(
            Text.assemble(_style.key("Layout: "), layout_name or "(none)")
        )


def _info_object(app: AppCtx, object_id: str) -> None:
    root, _fs = open_root(app)

    obj = root.get_object(object_id)
    if not obj.exists:
        raise ObjectNotFound(object_id)

    inv = obj.inventory.inventory
    num_versions = len(inv.versions)

    if app.json:
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
        app.console.print(Text.assemble(_style.key("ID:        "), inv.id))
        app.console.print(Text.assemble(_style.key("Spec:      "), str(obj.spec)))
        app.console.print(
            Text.assemble(_style.key("Algorithm: "), inv.digest_algorithm)
        )
        app.console.print(Text.assemble(_style.key("Head:      "), inv.head))
        app.console.print(Text.assemble(_style.key("Versions:  "), str(num_versions)))
