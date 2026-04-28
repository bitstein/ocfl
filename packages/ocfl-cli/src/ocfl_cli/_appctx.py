"""Typed view of `ctx.obj` factory entries.

`ctx.obj` itself remains a dict (typed AppCtx replacement is a follow-up).
This module names the factory callables so service / commands / tests
agree on the shape: production registers `default_sp_factory` /
`default_btc_factory`; tests inject fakes via `runner.invoke(obj=...)`.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ocfl_cli.config import Config
    from ocfl_cli.ots.bitcoin import BitcoinRPC
    from ocfl_cli.ots.simpleproof import SimpleProofClient


SpClientFactory = Callable[["Config"], "SimpleProofClient"]
BtcRpcFactory = Callable[["Config"], "BitcoinRPC"]


@dataclass(frozen=True, slots=True)
class AppCtx:
    sp_client_factory: SpClientFactory
    btc_rpc_factory: BtcRpcFactory
