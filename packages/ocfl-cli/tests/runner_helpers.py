from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from typer import Typer
from typer.testing import CliRunner, Result

from ocfl_cli.store.staging import StagingStore


class ObjRunner(CliRunner):
    def __init__(self) -> None:
        super().__init__()
        self.obj: dict[str, Any] = {}

    def invoke(
        self,
        app: Typer,
        args: str | Sequence[str] | None = None,
        input: bytes | str | None = None,
        env: Mapping[str, str | None] | None = None,
        catch_exceptions: bool = True,
        color: bool = False,
        **extra: Any,
    ) -> Result:
        extra.setdefault("obj", self.obj)
        return super().invoke(app, args, input, env, catch_exceptions, color, **extra)


@contextmanager
def injected(runner: ObjRunner, **ports: Any) -> Generator[None]:
    obj = runner.obj
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
