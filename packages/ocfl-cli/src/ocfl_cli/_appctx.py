"""Typed accessors for Typer's ``ctx.obj`` application state.

``ctx.obj`` remains the compatibility boundary for ``runner.invoke(obj=...)``
factory injection. Command bodies should consume this typed view instead of
reading string keys directly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import typer

    from ocfl_cli.config import ArchiveConfig, Config
    from ocfl_cli.ots.bitcoin import BitcoinRPC
    from ocfl_cli.ots.simpleproof import SimpleProofClient


SpClientFactory = Callable[["Config"], "SimpleProofClient"]
BtcRpcFactory = Callable[["Config"], "BitcoinRPC"]


class AppCtxError(RuntimeError):
    """Raised when Typer context application state is missing or malformed."""


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
) -> None:
    """Install application defaults into ``ctx.obj``.

    Factory values use ``setdefault`` so ``runner.invoke(obj=...)`` injection
    wins over production defaults.
    """
    obj = _obj(ctx)
    obj["root"] = root
    obj["config"] = config
    obj["archive"] = archive
    obj["json"] = json_output
    obj["quiet"] = quiet
    obj["verbose"] = verbose
    obj["no_color"] = no_color
    # Factories intentionally preserve runner.invoke(obj=...) test overrides.
    obj.setdefault("sp_client_factory", sp_client_factory)
    obj.setdefault("btc_rpc_factory", btc_rpc_factory)


def app_ctx(ctx: typer.Context) -> AppCtx:
    """Return the typed application context view backed by ``ctx.obj``."""
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
    )


def command_path(ctx: typer.Context) -> str:
    """Return Click's command path without the root program name."""
    prog = ctx.find_root().info_name
    if not prog:
        return ctx.command_path
    return ctx.command_path.removeprefix(f"{prog} ")


def root(ctx: typer.Context) -> str | None:
    return cast("str | None", _required(ctx, "root"))


def config(ctx: typer.Context) -> Config:
    return cast("Config", _required(ctx, "config"))


def archive(ctx: typer.Context) -> ArchiveConfig | None:
    return cast("ArchiveConfig | None", _required(ctx, "archive"))


def json_mode(ctx: typer.Context) -> bool:
    return cast("bool", _required(ctx, "json"))


def sp_client_factory(ctx: typer.Context) -> SpClientFactory:
    return cast("SpClientFactory", _required(ctx, "sp_client_factory"))


def btc_rpc_factory(ctx: typer.Context) -> BtcRpcFactory:
    return cast("BtcRpcFactory", _required(ctx, "btc_rpc_factory"))
