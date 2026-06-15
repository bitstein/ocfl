"""ocfl diff command."""

from typing import Annotated

import typer
from rich.text import Text

from ocfl_cli import _appctx as appctx
from ocfl_cli import _style
from ocfl_cli._common import open_root, output_json, parse_version
from ocfl_cli._exit import exit_for_error
from ocfl_lib import ObjectNotFound, OcflError

_ACTION_SYMBOLS = {"added": "A", "deleted": "D", "modified": "M", "renamed": "R"}
_SYM_STYLE = {
    "A": _style.added,
    "D": _style.deleted,
    "M": _style.modified,
    "R": _style.renamed,
}


def diff(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    v1: Annotated[str, typer.Argument(help="First version (e.g. v1 or 1).")],
    v2: Annotated[str, typer.Argument(help="Second version (e.g. v2 or 2).")],
) -> None:
    """Show changes between two object versions."""
    as_json = appctx.json_mode(ctx)
    root, _fs = open_root(appctx.root(ctx))

    try:
        obj = root.get_object(object_id)
        if not obj.exists:
            raise ObjectNotFound(object_id)
        vnum1 = parse_version(v1)
        vnum2 = parse_version(v2)
        entries = obj.diff_versions(vnum1, vnum2)
    except OcflError as e:
        exit_for_error(e)

    if as_json:
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
            styler = _SYM_STYLE.get(sym)
            styled_sym = styler(sym) if styler else Text(sym)
            parts: list = [styled_sym, "  ", e.path]
            if e.renamed_from:
                parts.append(f"  (from {e.renamed_from})")
            _style.echo(Text.assemble(*parts))
