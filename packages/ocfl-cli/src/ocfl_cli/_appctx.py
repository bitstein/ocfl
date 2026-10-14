"""Typed accessors for Typer's ``ctx.obj`` application state.

``ctx.obj`` remains the compatibility boundary for ``runner.invoke(obj=...)``
factory injection. Command bodies should consume this typed view instead of
reading string keys directly.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, cast

from ocfl_cli._style import make_console
from ocfl_cli.ports import DEFAULT_LOCK_TIMEOUT_SECONDS, Runtime

if TYPE_CHECKING:
    import typer
    from rich.console import Console

    from ocfl_cli.config import ArchiveConfig, Config
    from ocfl_cli.errors import ConfigError
    from ocfl_cli.ports import (
        BitcoinRPC,
        BlockStreamFactory,
        BtcRpcFactory,
        Clock,
        FsFactory,
        SimpleProofClient,
        Sleeper,
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
    verbose: bool
    no_color: bool
    sp_client_factory: SpClientFactory
    btc_rpc_factory: BtcRpcFactory
    clock: Clock
    sleeper: Sleeper
    block_stream_factory: BlockStreamFactory
    lock_timeout: float
    fs_factory: FsFactory
    console: Console
    err_console: Console


_SP_FACTORY_SOURCE = "_sp_client_factory_source"
_BTC_FACTORY_SOURCE = "_btc_rpc_factory_source"
_SP_FACTORY_WRAPPER = "_sp_client_factory_wrapper"
_BTC_FACTORY_WRAPPER = "_btc_rpc_factory_wrapper"
_CLIENT_LIFECYCLE = "client_lifecycle"


@dataclass(slots=True)
class ClientLifecycle:
    sp_source: SpClientFactory
    btc_source: BtcRpcFactory

    _missing: ClassVar[object] = object()
    _sp_client: object = _missing
    _btc_client: object = _missing
    _closed: bool = False

    def sp_client_factory(self, cfg: Config) -> SimpleProofClient:
        if self._sp_client is self._missing:
            self._sp_client = self.sp_source(cfg)
        return cast("SimpleProofClient", self._sp_client)

    def btc_rpc_factory(self, cfg: Config) -> BitcoinRPC:
        if self._btc_client is self._missing:
            self._btc_client = self.btc_source(cfg)
        return cast("BitcoinRPC", self._btc_client)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for client in (self._sp_client, self._btc_client):
            if client is self._missing:
                continue
            close = getattr(client, "close", None)
            if callable(close):
                close()


def _factory_source[T](
    obj: dict[str, object],
    *,
    source_key: str,
    wrapper_key: str,
    factory_key: str,
    default: T,
) -> T:
    """Return the original factory source for a possibly reused ``ctx.obj``."""
    candidate = obj.get(factory_key, default)
    if candidate is obj.get(wrapper_key):
        return cast("T", obj.get(source_key, default))
    return cast("T", candidate)


def _obj(ctx: typer.Context) -> dict[str, object]:
    obj = ctx.obj
    if not isinstance(obj, dict):
        raise AppCtxError("internal error: expected ctx.obj to be a dict")
    return cast("dict[str, object]", obj)


def _required(ctx: typer.Context, key: str) -> object:
    obj = _obj(ctx)
    if key not in obj:
        raise AppCtxError(f"internal error: missing ctx.obj[{key!r}]")
    return obj[key]


def install_app_ctx(
    ctx: typer.Context,
    *,
    root: str | None,
    config: Config,
    archive: ArchiveConfig | None,
    json_output: bool,
    quiet: bool,
    verbose: bool,
    no_color: bool,
    sp_client_factory: SpClientFactory,
    btc_rpc_factory: BtcRpcFactory,
    clock: Clock,
    sleeper: Sleeper,
    block_stream_factory: BlockStreamFactory,
    fs_factory: FsFactory,
) -> None:
    """Install application defaults into ``ctx.obj``.

    Factory source values preserve ``runner.invoke(obj=...)`` injection, then
    get wrapped in a per-invocation cache registered with Click's close hooks.
    The ports (``clock``, ``sleeper``, ``block_stream_factory``) and
    ``lock_timeout`` keep any value a test injected the same way.
    """
    obj = _obj(ctx)
    obj["root"] = root
    obj["config"] = config
    obj["archive"] = archive
    obj["json"] = json_output
    obj["quiet"] = quiet
    obj["verbose"] = verbose
    obj["no_color"] = no_color
    obj["console"] = make_console(no_color=no_color)
    obj["err_console"] = make_console(stderr=True, no_color=no_color)
    obj.setdefault("clock", clock)
    obj.setdefault("sleeper", sleeper)
    obj.setdefault("block_stream_factory", block_stream_factory)
    obj.setdefault("lock_timeout", DEFAULT_LOCK_TIMEOUT_SECONDS)
    obj.setdefault("fs_factory", fs_factory)
    # Factory sources intentionally preserve runner.invoke(obj=...) test overrides.
    sp_source = _factory_source(
        obj,
        source_key=_SP_FACTORY_SOURCE,
        wrapper_key=_SP_FACTORY_WRAPPER,
        factory_key="sp_client_factory",
        default=sp_client_factory,
    )
    btc_source = _factory_source(
        obj,
        source_key=_BTC_FACTORY_SOURCE,
        wrapper_key=_BTC_FACTORY_WRAPPER,
        factory_key="btc_rpc_factory",
        default=btc_rpc_factory,
    )
    obj[_SP_FACTORY_SOURCE] = sp_source
    obj[_BTC_FACTORY_SOURCE] = btc_source

    lifecycle = ClientLifecycle(sp_source=sp_source, btc_source=btc_source)
    sp_wrapper = lifecycle.sp_client_factory
    btc_wrapper = lifecycle.btc_rpc_factory
    obj["sp_client_factory"] = sp_wrapper
    obj["btc_rpc_factory"] = btc_wrapper
    obj[_SP_FACTORY_WRAPPER] = sp_wrapper
    obj[_BTC_FACTORY_WRAPPER] = btc_wrapper
    obj[_CLIENT_LIFECYCLE] = lifecycle
    ctx.call_on_close(lifecycle.close)


def app_ctx(ctx: typer.Context) -> AppCtx:
    return AppCtx(
        root=cast("str | None", _required(ctx, "root")),
        config=cast("Config", _required(ctx, "config")),
        archive=cast("ArchiveConfig | None", _required(ctx, "archive")),
        json=cast("bool", _required(ctx, "json")),
        quiet=cast("bool", _required(ctx, "quiet")),
        verbose=cast("bool", _required(ctx, "verbose")),
        no_color=cast("bool", _required(ctx, "no_color")),
        sp_client_factory=cast("SpClientFactory", _required(ctx, "sp_client_factory")),
        btc_rpc_factory=cast("BtcRpcFactory", _required(ctx, "btc_rpc_factory")),
        clock=cast("Clock", _required(ctx, "clock")),
        sleeper=cast("Sleeper", _required(ctx, "sleeper")),
        block_stream_factory=cast(
            "BlockStreamFactory", _required(ctx, "block_stream_factory")
        ),
        lock_timeout=cast("float", _required(ctx, "lock_timeout")),
        fs_factory=fs_factory(ctx),
        console=console(ctx),
        err_console=err_console(ctx),
    )


def command_path(ctx: typer.Context) -> str:
    prog = ctx.find_root().info_name
    if not prog:
        return ctx.command_path
    return ctx.command_path.removeprefix(f"{prog} ")


def root(ctx: typer.Context) -> str | None:
    return cast("str | None", _required(ctx, "root"))


def config(ctx: typer.Context) -> Config:
    return cast("Config", _required(ctx, "config"))


def config_error(ctx: typer.Context) -> ConfigError | None:
    """Why the config did not load, for the commands that run anyway."""
    return cast("ConfigError | None", _obj(ctx).get("config_error"))


def archive(ctx: typer.Context) -> ArchiveConfig | None:
    return cast("ArchiveConfig | None", _required(ctx, "archive"))


def json_mode(ctx: typer.Context) -> bool:
    return cast("bool", _required(ctx, "json"))


def verbose(ctx: typer.Context) -> bool:
    return cast("bool", _required(ctx, "verbose"))


def sp_client_factory(ctx: typer.Context) -> SpClientFactory:
    return cast("SpClientFactory", _required(ctx, "sp_client_factory"))


def btc_rpc_factory(ctx: typer.Context) -> BtcRpcFactory:
    return cast("BtcRpcFactory", _required(ctx, "btc_rpc_factory"))


def fs_factory(ctx: typer.Context) -> FsFactory:
    return cast("FsFactory", _required(ctx, "fs_factory"))


def runtime(ctx: typer.Context) -> Runtime:
    return Runtime(
        clock=cast("Clock", _required(ctx, "clock")),
        sleeper=cast("Sleeper", _required(ctx, "sleeper")),
        block_streams=cast(
            "BlockStreamFactory", _required(ctx, "block_stream_factory")
        ),
        lock_timeout=cast("float", _required(ctx, "lock_timeout")),
    )


def console(ctx: typer.Context) -> Console:
    return cast("Console", _required(ctx, "console"))


def err_console(ctx: typer.Context) -> Console:
    return cast("Console", _required(ctx, "err_console"))
