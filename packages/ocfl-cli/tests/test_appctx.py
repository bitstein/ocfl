from typing import cast

import pytest
import typer
from typer.core import TyperCommand

from ocfl_cli import _appctx as appctx
from ocfl_cli.config import Config
from ocfl_cli.errors import ConfigError
from ocfl_cli.main import production_ports
from ocfl_cli.ports import BtcRpcFactory, Ports, SpClientFactory


def _start(ports: Ports, config: Config | ConfigError) -> typer.Context:
    ctx = typer.Context(TyperCommand("probe"))
    ctx.obj = appctx.start(
        ctx, ports, config, root_option=None, json=True, quiet=False, no_color=False
    )
    return ctx


class _Client:
    def __init__(self, name: str, closed: list[str]) -> None:
        self.name = name
        self.closed = closed

    def close(self) -> None:
        self.closed.append(self.name)


def test_clients_are_cached_per_invocation_and_closed_with_the_ctx() -> None:
    cfg = Config()
    closed: list[str] = []
    created: list[_Client] = []

    def factory(_cfg: Config) -> _Client:
        client = _Client(f"client-{len(created)}", closed)
        created.append(client)
        return client

    ports = production_ports()
    ports.sp_client_factory = cast("SpClientFactory", factory)
    ports.btc_rpc_factory = cast("BtcRpcFactory", factory)
    ctx = _start(ports, cfg)
    app = appctx.of(ctx)

    assert app.sp_client_factory(cfg) is app.sp_client_factory(cfg)
    assert app.btc_rpc_factory(cfg) is app.btc_rpc_factory(cfg)
    assert len(created) == 2
    assert closed == []

    ctx.close()

    assert closed == ["client-0", "client-1"]


def test_fake_clients_without_close_are_fine() -> None:
    cfg = Config()
    client = object()
    ports = production_ports()
    ports.sp_client_factory = cast("SpClientFactory", lambda _cfg: client)
    ports.btc_rpc_factory = cast("BtcRpcFactory", lambda _cfg: client)
    ctx = _start(ports, cfg)
    app = appctx.of(ctx)

    assert app.sp_client_factory(cfg) is client
    assert app.btc_rpc_factory(cfg) is client
    ctx.close()


def test_one_ports_reused_across_invocations_gets_a_fresh_cache_each() -> None:
    cfg = Config()
    created: list[object] = []

    def factory(_cfg: Config) -> object:
        client = object()
        created.append(client)
        return client

    ports = production_ports()
    ports.sp_client_factory = cast("SpClientFactory", factory)

    for _ in range(2):
        ctx = _start(ports, cfg)
        sp_factory = appctx.of(ctx).sp_client_factory
        assert sp_factory(cfg) is sp_factory(cfg)
        ctx.close()

    assert len(created) == 2
    assert ports.sp_client_factory is factory


def test_a_broken_config_raises_from_of_and_is_returned_when_tolerated() -> None:
    error = ConfigError("bad toml")
    ctx = _start(production_ports(), error)

    with pytest.raises(ConfigError) as exc_info:
        appctx.of(ctx)

    assert exc_info.value is error
    assert appctx.tolerating_broken_config(ctx) == appctx.BrokenConfig(
        error=error, json=True
    )


def test_of_rejects_a_ctx_the_root_callback_did_not_fill() -> None:
    ctx = typer.Context(TyperCommand("probe"))

    with pytest.raises(appctx.AppCtxError):
        appctx.of(ctx)


def test_command_path_strips_click_root_program_name() -> None:
    root_ctx = typer.Context(TyperCommand("ocfl"), info_name="ocfl")
    stage_ctx = typer.Context(TyperCommand("stage"), info_name="stage", parent=root_ctx)
    content_ctx = typer.Context(
        TyperCommand("content"), info_name="content", parent=stage_ctx
    )

    assert content_ctx.command_path == "ocfl stage content"
    assert appctx.command_path(content_ctx) == "stage content"
