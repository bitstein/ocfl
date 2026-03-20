"""Shared CLI options, error handling, and output formatting."""

import json
import os
import sys
from pathlib import Path
from typing import Any

import typer

from ocfl_lib import LocalFS, StorageRoot, VNum


def resolve_root(root: str | None) -> Path:
    """Resolve storage root path from flag, env var, or cwd."""
    if root is not None:
        return Path(root).resolve()
    env = os.environ.get("OCFL_ROOT")
    if env:
        return Path(env).resolve()
    return Path.cwd()


def open_root(root: str | None) -> tuple[StorageRoot, LocalFS]:
    """Open an existing storage root. Exits on error."""
    path = resolve_root(root)
    try:
        fs = LocalFS(path)
        sr = StorageRoot(fs)
        return sr, fs
    except (ValueError, OSError) as e:
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
    sys.stderr.write(f"Error: {msg}\n")
