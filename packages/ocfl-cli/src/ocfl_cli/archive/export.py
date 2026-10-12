"""ocfl export command."""

import fnmatch
import sys
from pathlib import Path, PurePosixPath
from typing import Annotated

import typer

from ocfl_cli import _appctx as appctx
from ocfl_cli._common import open_root, parse_version
from ocfl_cli.errors import CLIError
from ocfl_lib import ObjectNotFound, to_path_map


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

    obj = root.get_object(object_id)
    if not obj.exists:
        raise ObjectNotFound(object_id)
    v = parse_version(version) if version else obj.head
    state = obj.version_info(v).state

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

    out_dir = Path(to).resolve()
    for logical_path in paths:
        try:
            dest = _export_destination(out_dir, logical_path)
        except ValueError as exc:
            raise CLIError(str(exc)) from exc

        if dest.exists() and not replace:
            raise CLIError(f"file exists: {dest} (use --replace to overwrite)")

        dest.parent.mkdir(parents=True, exist_ok=True)
        with obj.open_logical_path(v, logical_path) as f:
            dest.write_bytes(f.read())


def _export_destination(out_dir: Path, logical_path: str) -> Path:
    """Resolve a logical path to a contained local export destination."""
    logical = PurePosixPath(logical_path)
    segments = logical_path.split("/")
    if (
        not logical_path
        or logical.is_absolute()
        or any(segment in {"", ".", ".."} for segment in segments)
    ):
        raise ValueError(f"invalid logical path for export: {logical_path!r}")

    dest = out_dir.joinpath(*segments).resolve()
    if not dest.is_relative_to(out_dir):
        raise ValueError(f"invalid logical path for export: {logical_path!r}")
    return dest
