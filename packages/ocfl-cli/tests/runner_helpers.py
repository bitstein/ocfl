from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from typer.testing import CliRunner

from ocfl_cli.store.staging import StagingStore


def runner_obj(runner: CliRunner) -> dict[str, Any]:
    return cast("dict[str, Any]", runner.obj)  # ty: ignore[unresolved-attribute]


@contextmanager
def injected(runner: CliRunner, **ports: Any) -> Generator[None]:
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


def stage_args(root: Path, staging: Path, *args: str) -> list[str]:
    return ["--root", str(root), "stage", "--staging-root", str(staging), *args]


def staging_dir(root: Path, staging: Path, object_id: str) -> Path:
    return StagingStore(staging).staging_path(str(root.resolve()), object_id)
