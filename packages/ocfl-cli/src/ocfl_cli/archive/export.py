"""ocfl export command."""

import fnmatch
import sys
from pathlib import Path
from typing import Annotated

import typer

from ocfl_cli import _appctx as appctx
from ocfl_cli._common import open_root, parse_version
from ocfl_cli._exit import exit_for_error
from ocfl_cli._io import err
from ocfl_lib import ObjectNotFound, OcflError
from ocfl_lib.digest_map import to_path_map


def export(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    version: Annotated[
        str | None,
        typer.Option("--version", "-V", help="Version (default: head)."),
    ] = None,
    to: Annotated[
        str,
        typer.Option("--to", help="Output directory, or '-' for stdout."),
    ] = ".",
    file: Annotated[
        list[str] | None,
        typer.Option("--file", help="Glob pattern to filter files (repeatable)."),
    ] = None,
    replace: Annotated[
        bool,
        typer.Option("--replace", help="Overwrite existing files."),
    ] = False,
) -> None:
    """Export object content to local filesystem or stdout."""
    root, _fs = open_root(appctx.root(ctx))

    try:
        obj = root.get_object(object_id)
        if not obj.exists:
            raise ObjectNotFound(object_id)
    except OcflError as e:
        exit_for_error(e)

    v = parse_version(version) if version else obj.head
    state = obj.version_state(v)
    path_map = to_path_map(state)
    paths = sorted(path_map)

    # Filter by glob patterns if specified
    if file:
        paths = [p for p in paths if any(fnmatch.fnmatch(p, pat) for pat in file)]

    if to == "-":
        # Stdout: output first match silently
        if not paths:
            return
        with obj.open_logical_path(v, paths[0]) as f:
            sys.stdout.buffer.write(f.read())
        return

    out_dir = Path(to)
    for logical_path in paths:
        dest = out_dir / logical_path
        if dest.exists() and not replace:
            err(f"file exists: {dest} (use --replace to overwrite)")
            raise typer.Exit(1)

        dest.parent.mkdir(parents=True, exist_ok=True)
        with obj.open_logical_path(v, logical_path) as f:
            dest.write_bytes(f.read())
