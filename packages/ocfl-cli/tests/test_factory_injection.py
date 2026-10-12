"""Factory-injection seam contract.

Pins the seam: callers (production `main`, tests via
``runner.invoke(obj=...)``) supply ``sp_client_factory`` /
``btc_rpc_factory`` callables on ``ctx.obj``; service consumes them via
``factory(cfg)`` instead of constructing clients directly.
"""

from pathlib import Path

import pytest
import typer
from fakes import FakeSimpleProof
from typer.testing import CliRunner

from ocfl_cli._appctx import app_ctx
from ocfl_cli.main import app
from ocfl_cli.model.phase import Phase
from ocfl_cli.store.ots_state import load_ots
from ocfl_cli.store.staging import StagingStore
from ocfl_lib import LocalFS, Spec, StorageRoot, get_layout

_SP_OK = """\
[simpleproof]
base_url = "https://stage.example"
proof_category_id = 641
"""

_BTC_OK = """\
[bitcoin]
rpc_url = "http://127.0.0.1:8332"
zmq_endpoint = "tcp://127.0.0.1:28332"
confirmations = 3
"""


def _write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "ocfl-config" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(_SP_OK + _BTC_OK)
    cfg.chmod(0o600)
    monkeypatch.setenv("OCFL_CONFIG", str(cfg))


def _stage_args(root: Path, staging: Path, *args: str) -> list[str]:
    return ["--root", str(root), "stage", "--staging-root", str(staging), *args]


@pytest.fixture
def empty_sha256_root(tmp_path: Path) -> Path:
    location = tmp_path / "root"
    location.mkdir()
    fs = LocalFS(location)
    layout = get_layout("0002-flat-direct-storage-layout")
    StorageRoot.init(fs, fs, spec=Spec.V1_1, layout=layout)
    return location


def test_sp_factory_from_ctx_obj_consumed_by_timestamp_content(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sp_client_factory`` injected via ``runner.invoke(obj=...)`` drives
    ``stage timestamp content`` — the production default factory MUST NOT
    be called when an injected one is present."""
    _write_config(tmp_path, monkeypatch)

    calls: list[str] = []

    def factory(cfg) -> FakeSimpleProof:
        calls.append("called")
        return sp_fake

    obj = {"sp_client_factory": factory}
    staging = tmp_path / "staging"

    r = runner.invoke(
        app,
        _stage_args(empty_sha256_root, staging, "new", "obj:fac", "--ots"),
        obj=obj,
    )
    assert r.exit_code == 0, r.output

    src = tmp_path / "a.txt"
    src.write_text("alpha")
    r = runner.invoke(
        app,
        _stage_args(empty_sha256_root, staging, "add", "obj:fac", str(src)),
        obj=obj,
    )
    assert r.exit_code == 0, r.output

    r = runner.invoke(
        app,
        _stage_args(empty_sha256_root, staging, "timestamp", "content", "obj:fac"),
        obj=obj,
    )
    assert r.exit_code == 0, r.output

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
    """Production path: ``main`` callback installs default factories on
    ``ctx.obj`` so the stage commands have something to call."""
    _write_config(tmp_path, monkeypatch)

    seen: dict[str, object] = {}

    @app.command("_factory_probe")
    def _probe(ctx: typer.Context) -> None:
        typed = app_ctx(ctx)
        seen["sp"] = typed.sp_client_factory
        seen["btc"] = typed.btc_rpc_factory

    try:
        r = runner.invoke(app, ["_factory_probe"])
        assert r.exit_code == 0, r.output
        assert callable(seen["sp"]), "main must install default sp_client_factory"
        assert callable(seen["btc"]), "main must install default btc_rpc_factory"
    finally:
        app.registered_commands = [
            c for c in app.registered_commands if c.name != "_factory_probe"
        ]
