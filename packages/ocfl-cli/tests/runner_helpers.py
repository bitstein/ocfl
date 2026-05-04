"""Typed helpers for the custom CLI runner fixture."""

from typing import Any, cast

from typer.testing import CliRunner


def runner_obj(runner: CliRunner) -> dict[str, Any]:
    """Return the per-test ``ctx.obj`` dict attached by ``conftest.runner``."""
    return cast("dict[str, Any]", runner.obj)  # type: ignore[attr-defined]
