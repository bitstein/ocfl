from pathlib import Path

import pytest
import typer
from fakes import FakeSimpleProof
from ots_helpers import write_config
from runner_helpers import ObjRunner, stage_args
from typer.testing import CliRunner

from ocfl_cli import _appctx as appctx
from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app, production_ports
from ocfl_cli.model.phase import Phase
from ocfl_cli.store.ots_state import load_ots
from ocfl_cli.store.staging import StagingStore


def test_injected_sp_factory_consumed_by_timestamp_content(
    runner: ObjRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)

    calls: list[str] = []

    def factory(cfg) -> FakeSimpleProof:
        calls.append("called")
        return sp_fake

    ports = production_ports()
    ports.sp_client_factory = factory
    staging = tmp_path / "staging"

    r = runner.invoke(
        app,
        stage_args(empty_sha256_root, staging, "new", "obj:fac", "--ots"),
        obj=ports,
    )
    assert r.exit_code == ExitCode.SUCCESS, r.output

    src = tmp_path / "a.txt"
    src.write_text("alpha")
    r = runner.invoke(
        app,
        stage_args(empty_sha256_root, staging, "add", "obj:fac", str(src)),
        obj=ports,
    )
    assert r.exit_code == ExitCode.SUCCESS, r.output

    r = runner.invoke(
        app,
        stage_args(
            empty_sha256_root, staging, "timestamp", "obj:fac", "--expect", "content"
        ),
        obj=ports,
    )
    assert r.exit_code == ExitCode.SUCCESS, r.output

    assert calls, "injected sp_client_factory was never called"
    assert len(sp_fake._submissions) == 1

    store = StagingStore(staging)
    sd = store.staging_path(str(empty_sha256_root.resolve()), "obj:fac")
    state = load_ots(sd)
    assert state.phase is Phase.CONTENT_PENDING


def test_default_factories_when_obj_is_omitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)

    seen: dict[str, object] = {}

    @app.command("_factory_probe")
    def _probe(ctx: typer.Context) -> None:
        state = appctx.of(ctx)
        seen["sp"] = state.sp_client_factory
        seen["btc"] = state.btc_rpc_factory

    try:
        r = CliRunner().invoke(app, ["_factory_probe"])
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert callable(seen["sp"]), "main must install default sp_client_factory"
        assert callable(seen["btc"]), "main must install default btc_rpc_factory"
    finally:
        app.registered_commands = [
            c for c in app.registered_commands if c.name != "_factory_probe"
        ]
