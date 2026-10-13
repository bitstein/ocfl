"""Shared CLI options, error handling, and output formatting."""

import json
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ocfl_cli import _appctx as appctx
from ocfl_cli.errors import CLIError
from ocfl_lib import StorageRoot, VNum
from ocfl_lib.fs import open_fs

if TYPE_CHECKING:
    import httpx2 as httpx
    import typer

    from ocfl_cli.ports import FsFactory
    from ocfl_lib.fs import LocalFS
    from ocfl_lib.fs.s3 import S3FS

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


# The production storage filesystem: the lib's local-path / `s3://` factory.
DEFAULT_FS_FACTORY: FsFactory = open_fs


def open_location(ctx: typer.Context, location: str) -> LocalFS | S3FS:
    """The filesystem at `location`, through this run's `fs_factory` port."""
    return appctx.fs_factory(ctx)(location)


def open_root(ctx: typer.Context) -> tuple[StorageRoot, Any]:
    """Open this run's existing storage root."""
    fs = open_location(ctx, resolve_root(appctx.root(ctx)))
    return StorageRoot.open(fs), fs


def parse_version(v: str) -> VNum:
    """Parse version string, accepting both 'v1' and '1'."""
    if v.isdigit():
        v = f"v{v}"
    try:
        return VNum.parse(v)
    except ValueError:
        raise CLIError(f"invalid version: {v!r}") from None


def output_json(data: Any) -> None:
    """Print JSON to stdout."""
    sys.stdout.write(json.dumps(data, indent=2))
    sys.stdout.write("\n")
