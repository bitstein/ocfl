"""Shared CLI options, error handling, and output formatting."""

import json
import logging
import os
import sys
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from ocfl_lib import LocalFS, OcflError, StorageRoot, VNum

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)


def _log_http(resp: httpx.Response) -> None:
    """Log method + URL + status + elapsed at DEBUG. Never logs headers."""
    logger.debug(
        "http %s %s -> %d (%.3fs)",
        resp.request.method,
        resp.request.url,
        resp.status_code,
        resp.elapsed.total_seconds(),
    )


def resolve_root(root: str | None) -> str:
    """Resolve storage root from flag, env var, or cwd. URIs pass through."""
    raw = root if root is not None else os.environ.get("OCFL_ROOT")
    if raw is None:
        return str(Path.cwd())
    if "://" in raw:
        return raw
    return str(Path(raw).resolve())


def _make_fs(location: str):
    """Create FS backend for *location* (local path or s3:// URI)."""
    if location.startswith("s3://"):
        parsed = urllib.parse.urlparse(location)
        bucket = parsed.hostname or ""
        prefix = parsed.path.lstrip("/")
        from ocfl_lib.fs.s3 import S3FS

        return S3FS(bucket, prefix)
    return LocalFS(Path(location))


def open_root(root: str | None) -> tuple[StorageRoot, Any]:
    """Open an existing storage root. Exits on error."""
    location = resolve_root(root)
    try:
        fs = _make_fs(location)
        sr = StorageRoot(fs)
        return sr, fs
    except (OcflError, OSError) as e:
        err(str(e))
        raise typer.Exit(1) from None


def parse_version(v: str) -> VNum:
    """Parse version string, accepting both 'v1' and '1'."""
    if v.isdigit():
        v = f"v{v}"
    try:
        return VNum.parse(v)
    except ValueError:
        err(f"invalid version: {v!r}")
        raise typer.Exit(1) from None


def output_json(data: Any) -> None:
    """Print JSON to stdout."""
    sys.stdout.write(json.dumps(data, indent=2))
    sys.stdout.write("\n")


def err(msg: str) -> None:
    """Print error message to stderr."""
    from ocfl_cli import _style

    _style.echo(_style.error(f"Error: {msg}"), err=True)
