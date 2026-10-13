"""Typed CLI context accessors."""

from typing import cast

import click
import pytest
import typer
from fakes import InMemoryHashBlockStream

from ocfl_cli._appctx import (
    AppCtxError,
    app_ctx,
    btc_rpc_factory,
    command_path,
    install_app_ctx,
    json_mode,
    root,
    sp_client_factory,
    verbose,
)
from ocfl_cli._common import DEFAULT_FS_FACTORY
from ocfl_cli._style import make_console
from ocfl_cli.config import ArchiveConfig, Config
from ocfl_cli.ports import BtcRpcFactory, SpClientFactory
from ocfl_cli.system import SYSTEM_CLOCK, SYSTEM_SLEEPER


def _factory(_cfg: Config) -> object:
    return object()


def test_app_ctx_builds_typed_view_from_ctx_obj() -> None:
    archive = ArchiveConfig(name="main", root="/archive")
    cfg = Config(archives={"main": archive})
    out, err = make_console(), make_console(stderr=True)
    ctx = typer.Context(
        click.Command("probe"),
        obj={
            "root": "/archive",
            "config": cfg,
            "archive": archive,
            "json": True,
            "quiet": False,
            "verbose": True,
            "no_color": False,
            "sp_client_factory": _factory,
            "btc_rpc_factory": _factory,
            "clock": SYSTEM_CLOCK,
            "sleeper": SYSTEM_SLEEPER,
            "block_stream_factory": _factory,
            "lock_timeout": 30.0,
            "fs_factory": DEFAULT_FS_FACTORY,
            "console": out,
            "err_console": err,
        },
    )

    typed = app_ctx(ctx)

    assert typed.root == "/archive"
    assert typed.config is cfg
    assert typed.archive is archive
    assert typed.json is True
    assert typed.quiet is False
    assert typed.verbose is True
    assert typed.no_color is False
    assert typed.sp_client_factory is _factory
    assert typed.btc_rpc_factory is _factory
    assert typed.clock is SYSTEM_CLOCK
    assert typed.sleeper is SYSTEM_SLEEPER
    assert typed.block_stream_factory is _factory
    assert typed.lock_timeout == 30.0
    assert typed.fs_factory is DEFAULT_FS_FACTORY
    assert typed.console is out
    assert typed.err_console is err


def test_app_ctx_reports_missing_required_keys() -> None:
    ctx = typer.Context(click.Command("probe"), obj={})

    with pytest.raises(AppCtxError) as exc_info:
        app_ctx(ctx)

    assert "ctx.obj['root']" in str(exc_info.value)


def test_app_ctx_reports_malformed_ctx_obj() -> None:
    ctx = typer.Context(click.Command("probe"), obj=object())

    with pytest.raises(AppCtxError) as exc_info:
        app_ctx(ctx)

    assert "expected ctx.obj to be a dict" in str(exc_info.value)


def test_single_value_accessors_only_require_their_key() -> None:
    ctx = typer.Context(
        click.Command("probe"),
        obj={"root": "/archive", "json": True, "verbose": True},
    )

    assert root(ctx) == "/archive"
    assert json_mode(ctx) is True
    assert verbose(ctx) is True


def test_command_path_strips_click_root_program_name() -> None:
    root_ctx = typer.Context(click.Command("ocfl"), info_name="ocfl")
    stage_ctx = typer.Context(
        click.Command("stage"), info_name="stage", parent=root_ctx
    )
    content_ctx = typer.Context(
        click.Command("content"), info_name="content", parent=stage_ctx
    )

    assert content_ctx.command_path == "ocfl stage content"
    assert command_path(content_ctx) == "stage content"


def test_install_app_ctx_caches_and_closes_clients_per_invocation() -> None:
    archive = ArchiveConfig(name="main", root="/archive")
    cfg = Config(archives={"main": archive})
    closed: list[str] = []

    class Client:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            closed.append(self.name)

    created: list[Client] = []

    def factory(_cfg: Config) -> Client:
        client = Client(f"client-{len(created)}")
        created.append(client)
        return client

    ctx = typer.Context(click.Command("probe"), obj={})
    install_app_ctx(
        ctx,
        root="/archive",
        config=cfg,
        archive=archive,
        json_output=False,
        quiet=False,
        verbose=False,
        no_color=False,
        sp_client_factory=cast("SpClientFactory", factory),
        btc_rpc_factory=cast("BtcRpcFactory", factory),
        clock=SYSTEM_CLOCK,
        sleeper=SYSTEM_SLEEPER,
        block_stream_factory=lambda cfg: InMemoryHashBlockStream(),
        fs_factory=DEFAULT_FS_FACTORY,
    )

    sp_factory = sp_client_factory(ctx)
    btc_factory = btc_rpc_factory(ctx)

    assert sp_factory(cfg) is sp_factory(cfg)
    assert btc_factory(cfg) is btc_factory(cfg)
    assert len(created) == 2

    ctx.close()

    assert closed == ["client-0", "client-1"]


def test_install_app_ctx_accepts_fakes_without_close_method() -> None:
    cfg = Config()
    client = object()

    def factory(_cfg: Config) -> object:
        return client

    ctx = typer.Context(click.Command("probe"), obj={})
    install_app_ctx(
        ctx,
        root=None,
        config=cfg,
        archive=None,
        json_output=False,
        quiet=False,
        verbose=False,
        no_color=False,
        sp_client_factory=cast("SpClientFactory", factory),
        btc_rpc_factory=cast("BtcRpcFactory", factory),
        clock=SYSTEM_CLOCK,
        sleeper=SYSTEM_SLEEPER,
        block_stream_factory=lambda cfg: InMemoryHashBlockStream(),
        fs_factory=DEFAULT_FS_FACTORY,
    )

    assert sp_client_factory(ctx)(cfg) is client
    assert btc_rpc_factory(ctx)(cfg) is client
    ctx.close()


def test_install_app_ctx_reuses_injected_factory_source_across_invocations() -> None:
    cfg = Config()
    shared_obj: dict[str, object] = {}
    created: list[object] = []

    def factory(_cfg: Config) -> object:
        client = object()
        created.append(client)
        return client

    for _ in range(2):
        ctx = typer.Context(click.Command("probe"), obj=shared_obj)
        install_app_ctx(
            ctx,
            root=None,
            config=cfg,
            archive=None,
            json_output=False,
            quiet=False,
            verbose=False,
            no_color=False,
            sp_client_factory=cast("SpClientFactory", factory),
            btc_rpc_factory=cast("BtcRpcFactory", factory),
            clock=SYSTEM_CLOCK,
            sleeper=SYSTEM_SLEEPER,
            block_stream_factory=lambda cfg: InMemoryHashBlockStream(),
            fs_factory=DEFAULT_FS_FACTORY,
        )

        sp_factory = sp_client_factory(ctx)
        assert sp_factory(cfg) is sp_factory(cfg)
        ctx.close()

    assert len(created) == 2
