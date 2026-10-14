import importlib.metadata
import json
import logging
import os
import sys
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ocfl_cli.errors import CLIError
from ocfl_lib import StorageRoot, VNum

if TYPE_CHECKING:
    import httpx2 as httpx

    from ocfl_cli._appctx import AppCtx

logger = logging.getLogger(__name__)


def _log_http(resp: httpx.Response) -> None:
    logger.debug(
        "http %s %s -> %d (%.3fs)",
        resp.request.method,
        resp.request.url,
        resp.status_code,
        resp.elapsed.total_seconds(),
    )


def resolve_root(root: str | None) -> str:
    raw = root if root is not None else os.environ.get("OCFL_ROOT")
    if raw is None:
        return str(Path.cwd())
    if "://" in raw:
        return raw
    return str(Path(raw).resolve())


def open_root(app: AppCtx) -> tuple[StorageRoot, Any]:
    fs = app.fs_factory(resolve_root(app.root))
    return StorageRoot.open(fs), fs


def parse_version(v: str) -> VNum:
    if v.isdigit():
        v = f"v{v}"
    try:
        return VNum.parse(v)
    except ValueError:
        raise CLIError(f"invalid version: {v!r}") from None


@cache
def installed_cli_version() -> str:
    try:
        return importlib.metadata.version("ocfl-cli")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def output_json(data: Any) -> None:
    sys.stdout.write(json.dumps(data, indent=2))
    sys.stdout.write("\n")
