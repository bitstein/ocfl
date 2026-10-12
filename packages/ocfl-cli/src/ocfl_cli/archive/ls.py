"""ocfl ls command."""

from typing import TYPE_CHECKING, Annotated

import typer
from rich.text import Text

from ocfl_cli import _appctx as appctx
from ocfl_cli import _style
from ocfl_cli._common import open_root, output_json, parse_version
from ocfl_lib import ObjectNotFound, to_path_map

if TYPE_CHECKING:
    from ocfl_lib import VNum


def ls(
    ctx: typer.Context,
    object_id: Annotated[
        str | None, typer.Argument(help="Object ID (omit to list all objects).")
    ] = None,
    version: Annotated[
        str | None, typer.Option("--version", "-V", help="Version (e.g. v1 or 1).")
    ] = None,
    digests: Annotated[
        bool, typer.Option("--digests", help="Show digests alongside paths.")
    ] = False,
) -> None:
    """List objects in root, or files in an object version."""
    as_json = appctx.json_mode(ctx)

    if object_id is not None:
        _ls_object(ctx, object_id, version, digests, as_json)
    else:
        _ls_root(ctx, as_json)


def _ls_root(ctx: typer.Context, json_mode: bool) -> None:
    root, _fs = open_root(appctx.root(ctx))
    ids = [obj.id for obj in root.objects()]
    if json_mode:
        output_json(ids)
    else:
        for oid in ids:
            typer.echo(oid)


def _ls_object(
    ctx: typer.Context,
    object_id: str,
    version_str: str | None,
    digests: bool,
    json_mode: bool,
) -> None:
    root, _fs = open_root(appctx.root(ctx))

    obj = root.get_object(object_id)
    if not obj.exists:
        raise ObjectNotFound(object_id)
    v: VNum = parse_version(version_str) if version_str else obj.head
    state = obj.version_info(v).state

    path_map = to_path_map(state)  # path → digest

    if json_mode:
        entries = [
            {"path": p, "digest": d} if digests else {"path": p}
            for p, d in sorted(path_map.items())
        ]
        output_json(entries)
    else:
        for path, digest in sorted(path_map.items()):
            if digests:
                _style.echo(Text.assemble(_style.digest(digest), "  ", path))
            else:
                typer.echo(path)
