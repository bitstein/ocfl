from pathlib import Path

import pytest
import typer
from fakes import FakeSimpleProof
from ots_helpers import write_config
from runner_helpers import stage_args
from typer.testing import CliRunner

from ocfl_cli._appctx import app_ctx
from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app
from ocfl_cli.model.phase import Phase
from ocfl_cli.store.ots_state import load_ots
from ocfl_cli.store.staging import StagingStore


def test_sp_factory_from_ctx_obj_consumed_by_timestamp_content(
    runner: CliRunner,
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

    obj = {"sp_client_factory": factory}
    staging = tmp_path / "staging"

    r = runner.invoke(
        app,
        stage_args(empty_sha256_root, staging, "new", "obj:fac", "--ots"),
        obj=obj,
    )
    assert r.exit_code == ExitCode.SUCCESS, r.output

    src = tmp_path / "a.txt"
    src.write_text("alpha")
    r = runner.invoke(
        app,
        stage_args(empty_sha256_root, staging, "add", "obj:fac", str(src)),
        obj=obj,
    )
    assert r.exit_code == ExitCode.SUCCESS, r.output

    r = runner.invoke(
        app,
        stage_args(
            empty_sha256_root, staging, "timestamp", "obj:fac", "--expect", "content"
        ),
        obj=obj,
    )
    assert r.exit_code == ExitCode.SUCCESS, r.output

    assert calls, "injected sp_client_factory was never called"
    assert len(sp_fake._submissions) == 1

    store = StagingStore(staging)
    sd = store.staging_path(str(empty_sha256_root.resolve()), "obj:fac")
    state = load_ots(sd)
    assert state.phase is Phase.CONTENT_PENDING


def test_default_factories_registered_when_obj_omits_them(
    runner: CliRunner,
    empty_sha256_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)

    seen: dict[str, object] = {}

    @app.command("_factory_probe")
    def _probe(ctx: typer.Context) -> None:
        typed = app_ctx(ctx)
        seen["sp"] = typed.sp_client_factory
        seen["btc"] = typed.btc_rpc_factory

    try:
        r = runner.invoke(app, ["_factory_probe"])
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert callable(seen["sp"]), "main must install default sp_client_factory"
        assert callable(seen["btc"]), "main must install default btc_rpc_factory"
    finally:
        app.registered_commands = [
            c for c in app.registered_commands if c.name != "_factory_probe"
        ]
