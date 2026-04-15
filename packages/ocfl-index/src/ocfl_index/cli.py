"""ocfl-index command-line entrypoint."""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from ocfl_index import Index, db

if TYPE_CHECKING:
    from collections.abc import Iterable

app = typer.Typer(no_args_is_help=True, help="OCFL storage root index")

DEFAULT_DB = Path("./ocfl-index.db")


def _print_json(payload: object) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _resolve_root(db_path: Path) -> str:
    if not db_path.exists():
        typer.echo(f"index db not found: {db_path}", err=True)
        raise typer.Exit(code=2)
    conn = db.connect(db_path)
    root = db.meta_get(conn, "root_path")
    conn.close()
    if not root:
        typer.echo("index db has no recorded root_path", err=True)
        raise typer.Exit(code=2)
    return root


@app.command()
def init(
    root: Annotated[Path, typer.Option(..., help="Path to OCFL storage root")],
    db_path: Annotated[
        Path, typer.Option("--db", help="Path to index DB")
    ] = DEFAULT_DB,
) -> None:
    """Create a new index database for an OCFL root."""
    Index.open(db_path, root).close()
    typer.echo(f"initialized {db_path} for root {root}")


@app.command()
def refresh(
    full: Annotated[bool, typer.Option("--full", help="Truncate and rebuild")] = False,
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
) -> None:
    """Refresh the index against the OCFL root."""
    root = _resolve_root(db_path)
    try:
        with Index.open(db_path, root) as idx:
            report = idx.refresh(full=full)
    except Exception as exc:
        typer.echo(f"refresh failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(dataclasses.asdict(report), sort_keys=True))


@app.command()
def status(
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show row counts and last scan timestamps."""
    root = _resolve_root(db_path)
    with Index.open(db_path, root) as idx:
        counts = {
            table: idx.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "objects",
                "versions",
                "content_paths",
                "blobs",
                "ots_attestations",
                "ots_inventory",
            )
        }
        payload = {
            "root_path": root,
            "schema_version": db.meta_get(idx.conn, "schema_version"),
            "last_scan_at": db.meta_get(idx.conn, "last_scan_at"),
            "last_full_scan_at": db.meta_get(idx.conn, "last_full_scan_at"),
            "counts": counts,
        }
    if json_output:
        _print_json(payload)
        return
    typer.echo(f"root: {payload['root_path']}")
    typer.echo(f"schema_version: {payload['schema_version']}")
    typer.echo(f"last_scan_at: {payload['last_scan_at']}")
    typer.echo(f"last_full_scan_at: {payload['last_full_scan_at']}")
    for table, count in counts.items():
        typer.echo(f"  {table}: {count}")


@app.command()
def show(
    object_id: Annotated[str, typer.Argument()],
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Render an object: head versions, head paths."""
    root = _resolve_root(db_path)
    with Index.open(db_path, root) as idx:
        obj = idx.get_object(object_id)
        if obj is None:
            typer.echo(f"object {object_id!r} not in index", err=True)
            raise typer.Exit(code=1)
        versions = idx.conn.execute(
            "SELECT version_num, version, created_at FROM versions "
            "WHERE object_id = ? ORDER BY version_num",
            (object_id,),
        ).fetchall()
        paths = list(idx.list_paths(object_id, obj.head_version_num))
    payload = {
        "object": dataclasses.asdict(obj),
        "versions": [
            {"version_num": v[0], "version": v[1], "created_at": v[2]} for v in versions
        ],
        "head_paths": [dataclasses.asdict(p) for p in paths],
    }
    if json_output:
        _print_json(payload)
        return
    typer.echo(f"object_id: {obj.object_id}")
    typer.echo(f"head: {obj.head_version} ({obj.head_version_num})")
    typer.echo(f"digest_algorithm: {obj.digest_algorithm}")
    typer.echo("versions:")
    for v in versions:
        typer.echo(f"  v{v[0]}: {v[2]}")
    typer.echo("head paths:")
    for p in _sorted(paths):
        typer.echo(f"  {p.logical_path}")


def _sorted(rows: Iterable[object]) -> list:  # type: ignore[type-arg]
    return sorted(rows, key=lambda r: r.logical_path)  # type: ignore[attr-defined]


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    app()
