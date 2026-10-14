import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ocfl_cli._style import make_console
from ocfl_cli.errors import ConfigError

if TYPE_CHECKING:
    import typer
    from rich.console import Console

    from ocfl_cli.config import ArchiveConfig, Config
    from ocfl_cli.ports import (
        BitcoinRPC,
        BtcRpcFactory,
        FsFactory,
        Ports,
        Runtime,
        SimpleProofClient,
        SpClientFactory,
    )


class AppCtxError(RuntimeError): ...


@dataclass(frozen=True, slots=True)
class AppCtx:
    root: str | None
    config: Config
    archive: ArchiveConfig | None
    json: bool
    quiet: bool
    console: Console
    err_console: Console
    runtime: Runtime
    fs_factory: FsFactory
    sp_client_factory: SpClientFactory
    btc_rpc_factory: BtcRpcFactory


@dataclass(frozen=True, slots=True)
class BrokenConfig:
    error: ConfigError
    json: bool


@runtime_checkable
class Closeable(Protocol):
    def close(self) -> None: ...


@dataclass(slots=True)
class _Clients:
    sp_source: SpClientFactory
    btc_source: BtcRpcFactory
    _sp: SimpleProofClient | None = None
    _btc: BitcoinRPC | None = None

    def simpleproof(self, cfg: Config) -> SimpleProofClient:
        if self._sp is None:
            self._sp = self.sp_source(cfg)
        return self._sp

    def bitcoin(self, cfg: Config) -> BitcoinRPC:
        if self._btc is None:
            self._btc = self.btc_source(cfg)
        return self._btc

    def close(self) -> None:
        for client in (self._sp, self._btc):
            if isinstance(client, Closeable):
                client.close()
        self._sp = self._btc = None


def _locate(
    config: Config, root_option: str | None
) -> tuple[str | None, ArchiveConfig | None]:
    if root_option is not None:
        named = config.archive(root_option)
        if named is not None:
            return named.root, named
    elif os.environ.get("OCFL_ROOT") is None:
        implicit = config.implicit_default()
        if implicit is not None:
            return implicit.root, implicit
    return root_option, None


def start(
    ctx: typer.Context,
    ports: Ports,
    config: Config | ConfigError,
    *,
    root_option: str | None,
    json: bool,
    quiet: bool,
    no_color: bool,
) -> AppCtx | BrokenConfig:
    if isinstance(config, ConfigError):
        return BrokenConfig(error=config, json=json)
    root, archive = _locate(config, root_option)
    clients = _Clients(
        sp_source=ports.sp_client_factory, btc_source=ports.btc_rpc_factory
    )
    ctx.call_on_close(clients.close)
    return AppCtx(
        root=root,
        config=config,
        archive=archive,
        json=json,
        quiet=quiet,
        console=make_console(no_color=no_color),
        err_console=make_console(stderr=True, no_color=no_color),
        runtime=ports.runtime(),
        fs_factory=ports.fs_factory,
        sp_client_factory=clients.simpleproof,
        btc_rpc_factory=clients.bitcoin,
    )


def of(ctx: typer.Context) -> AppCtx:
    match ctx.obj:
        case AppCtx() as app:
            return app
        case BrokenConfig(error=error):
            raise error
        case other:
            raise AppCtxError(f"internal error: ctx.obj is {type(other).__name__}")


def tolerating_broken_config(ctx: typer.Context) -> AppCtx | BrokenConfig:
    match ctx.obj:
        case AppCtx() | BrokenConfig() as state:
            return state
        case other:
            raise AppCtxError(f"internal error: ctx.obj is {type(other).__name__}")


def command_path(ctx: typer.Context) -> str:
    prog = ctx.find_root().info_name
    if not prog:
        return ctx.command_path
    return ctx.command_path.removeprefix(f"{prog} ")
