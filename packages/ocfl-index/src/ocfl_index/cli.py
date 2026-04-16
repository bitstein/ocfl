"""ocfl-index command-line entrypoint."""

import dataclasses
import json
import logging
from pathlib import Path
from typing import Annotated

import typer

from ocfl_index import Index

app = typer.Typer(no_args_is_help=True, help="OCFL storage root index")

DEFAULT_DB = Path("./ocfl-index.db")


def _print_json(payload: object) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _require_db(db_path: Path) -> None:
    if not db_path.exists():
        typer.echo(f"index db not found: {db_path}", err=True)
        raise typer.Exit(code=2)


@app.command()
def init(
    root: Annotated[Path, typer.Option(..., help="Path to OCFL storage root")],
    db_path: Annotated[
        Path, typer.Option("--db", help="Path to index DB")
    ] = DEFAULT_DB,
) -> None:
    """Create a new index database for an OCFL root."""
    Index.open(db_path, root=root).close()
    typer.echo(f"initialized {db_path} for root {root}")


@app.command()
def refresh(
    full: Annotated[bool, typer.Option("--full", help="Truncate and rebuild")] = False,
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
) -> None:
    """Refresh the index against the OCFL root."""
    _require_db(db_path)
    try:
        with Index.open(db_path) as idx:
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
    _require_db(db_path)
    with Index.open(db_path) as idx:
        report = idx.status()
    if json_output:
        _print_json(dataclasses.asdict(report))
        return
    typer.echo(f"root: {report.root_path}")
    typer.echo(f"schema_version: {report.schema_version}")
    typer.echo(f"last_scan_at: {report.last_scan_at}")
    typer.echo(f"last_full_scan_at: {report.last_full_scan_at}")
    for table, count in report.counts.items():
        typer.echo(f"  {table}: {count}")


@app.command()
def show(
    object_id: Annotated[str, typer.Argument()],
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Render an object: head versions, head paths."""
    _require_db(db_path)
    with Index.open(db_path) as idx:
        detail = idx.get_object_detail(object_id)
    if detail is None:
        typer.echo(f"object {object_id!r} not in index", err=True)
        raise typer.Exit(code=1)
    if json_output:
        _print_json(dataclasses.asdict(detail))
        return
    obj = detail.object
    typer.echo(f"object_id: {obj.object_id}")
    typer.echo(f"head: {obj.head_version} ({obj.head_version_num})")
    typer.echo(f"digest_algorithm: {obj.digest_algorithm}")
    typer.echo("versions:")
    for v in detail.versions:
        typer.echo(f"  v{v.version_num}: {v.created_at}")
    typer.echo("head paths:")
    for p in detail.head_paths:
        typer.echo(f"  {p.logical_path}")


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    app()
