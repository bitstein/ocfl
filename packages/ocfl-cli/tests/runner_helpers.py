"""Typed helpers for the custom CLI runner fixture."""

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, cast

from typer.testing import CliRunner


def runner_obj(runner: CliRunner) -> dict[str, Any]:
    """Return the per-test ``ctx.obj`` dict attached by ``conftest.runner``."""
    return cast("dict[str, Any]", runner.obj)  # ty: ignore[unresolved-attribute]


@contextmanager
def injected(runner: CliRunner, **ports: Any) -> Generator[None]:
    """Inject `ports` (``ctx.obj`` keys) for the invocations inside the block."""
    obj = runner_obj(runner)
    missing = object()
    saved = {key: obj.get(key, missing) for key in ports}
    obj.update(ports)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is missing:
                obj.pop(key, None)
            else:
                obj[key] = value
