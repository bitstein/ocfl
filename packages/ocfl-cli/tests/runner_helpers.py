from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import typer
from typer import Typer
from typer.core import TyperCommand
from typer.testing import CliRunner, Result

from ocfl_cli import _appctx as appctx
from ocfl_cli.config import Config
from ocfl_cli.main import production_ports
from ocfl_cli.ports import Ports
from ocfl_cli.stage._app import StageCtx
from ocfl_cli.store.staging import StagingStore


class ObjRunner(CliRunner):
    def __init__(self) -> None:
        super().__init__()
        self.ports: Ports = production_ports()

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
        extra.setdefault("obj", self.ports)
        return super().invoke(app, args, input, env, catch_exceptions, color, **extra)


@contextmanager
def injected(runner: ObjRunner, **overrides: Any) -> Generator[None]:
    saved = {name: getattr(runner.ports, name) for name in overrides}
    for name, value in overrides.items():
        setattr(runner.ports, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(runner.ports, name, value)


def probe_ctx(
    *,
    ports: Ports | None = None,
    root: str | None = None,
    config: Config | None = None,
    staging: StagingStore | None = None,
    command_parts: tuple[str, ...] = ("probe",),
) -> typer.Context:
    root_ctx = typer.Context(TyperCommand("ocfl"), info_name="ocfl")
    app = appctx.start(
        root_ctx,
        ports or production_ports(),
        config if config is not None else Config(),
        root_option=root,
        json=False,
        quiet=False,
        no_color=False,
    )
    assert isinstance(app, appctx.AppCtx)
    root_ctx.obj = app
    ctx = root_ctx
    for part in command_parts:
        ctx = typer.Context(TyperCommand(part), info_name=part, parent=ctx)
    if staging is not None:
        ctx.obj = StageCtx.extend(app, staging)
    return ctx


def stage_args(root: Path, staging: Path, *args: str) -> list[str]:
    return ["--root", str(root), "stage", "--staging-root", str(staging), *args]


def staging_dir(root: Path, staging: Path, object_id: str) -> Path:
    return StagingStore(staging).staging_path(str(root.resolve()), object_id)
