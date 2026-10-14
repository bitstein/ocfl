from typing import Annotated

import typer
from rich.text import Text

from ocfl_cli import _appctx as appctx
from ocfl_cli import _style
from ocfl_cli._common import open_root, output_json, parse_version
from ocfl_lib import DiffEntry, ObjectNotFound, VNum, to_path_map

_ACTION_SYMBOLS = {"added": "A", "deleted": "D", "modified": "M", "renamed": "R"}
_SYM_STYLE = {
    "A": _style.added,
    "D": _style.deleted,
    "M": _style.modified,
    "R": _style.renamed,
}


def show(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    version: Annotated[
        str | None, typer.Argument(help="Version (default: head).")
    ] = None,
) -> None:
    """Show version metadata and diff vs previous."""
    app = appctx.of(ctx)
    root, _fs = open_root(app)

    obj = root.get_object(object_id)
    if not obj.exists:
        raise ObjectNotFound(object_id)
    v = parse_version(version) if version else obj.head
    info = obj.version_info(v)

    if v.number == 1:
        state = obj.version_info(v).state
        pm = to_path_map(state)
        changes = [
            DiffEntry(path=p, action="added", digest_after=d)
            for p, d in sorted(pm.items())
        ]
    else:
        prev_v = VNum(v.number - 1, v.padding)
        changes = obj.diff_versions(prev_v, v)

    if app.json:
        output_json(
            {
                "version": str(v),
                "created": info.created,
                "message": info.message,
                "user": (
                    {"name": info.user.name, "address": info.user.address}
                    if info.user
                    else None
                ),
                "changes": [
                    {"path": c.path, "action": c.action, "renamed_from": c.renamed_from}
                    for c in changes
                ],
            }
        )
    else:
        app.console.print(
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
        for c in changes:
            sym = _ACTION_SYMBOLS.get(c.action, "?")
            styler = _SYM_STYLE.get(sym)
            styled_sym = styler(sym) if styler else Text(sym)
            parts: list = ["  ", styled_sym, "  ", c.path]
            if c.renamed_from:
                parts.append(f"  (from {c.renamed_from})")
            app.console.print(Text.assemble(*parts))
