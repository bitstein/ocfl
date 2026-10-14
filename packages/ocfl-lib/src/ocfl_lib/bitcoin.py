"""A Bitcoin Core JSON-RPC client that is a `BlockHeaderSource` (`[bitcoin]` extra).

Install `ocfl-lib[bitcoin]` to use it. It is a thin sync `httpx2` wrapper
that retries transient faults. Authorization headers are scrubbed from any
propagated `httpx2` exception so credentials never leak through tracebacks
or log handlers.
"""

import contextlib
import logging
import time
from base64 import b64encode
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import httpx2 as httpx

from ocfl_lib._errors import (
    BitcoinNoBlockError,
    BitcoinRpcAuthError,
    BitcoinRpcError,
    BitcoinRpcTransientError,
)
from ocfl_lib.ots import ChainBlock

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_REDACTED = "<redacted>"
_SENSITIVE_HEADERS = ("authorization", "proxy-authorization", "cookie")


def _scrub_exc_request(exc: BaseException) -> None:
    # httpx2 stores the request on a private ``_request`` slot; the public
    # ``request`` property raises RuntimeError when unset, so read directly.
    request = getattr(exc, "_request", None)
    headers = getattr(request, "headers", None)
    if headers is None:
        return
    for name in _SENSITIVE_HEADERS:
        if name in headers:
            headers[name] = _REDACTED


@dataclass(frozen=True, slots=True)
class BlockchainInfo:
    height: int
    best_block_hash: str
    initial_block_download: bool
    verification_progress: float


@dataclass(frozen=True, slots=True)
class BlockHeader:
    """A header as bitcoind reports it; `merkle_root` is display (reversed) hex."""

    block_hash: str
    height: int
    confirmations: int
    merkle_root: str
    time: int


class BitcoinRpcClient:
    """Sync JSON-RPC client for Bitcoin Core; a `BlockHeaderSource`.

    Callers inject a configured `httpx2.Client` (auth, timeout), or use
    `connect`. Transient faults are retried `attempts` times, sleeping
    `min(60, 2 * 2**i)` seconds between attempts.
    """

    def __init__(
        self,
        rpc_url: str,
        *,
        client: httpx.Client,
        sleep: Callable[[float], None] = time.sleep,
        attempts: int = 3,
    ) -> None:
        if attempts < 1:
            raise BitcoinRpcError("attempts must be >= 1")
        self._rpc_url = rpc_url
        self._client = client
        self._sleep = sleep
        self._attempts = attempts

    @classmethod
    def connect(
        cls, rpc_url: str, *, user: str, password: str, timeout: float = 30.0
    ) -> BitcoinRpcClient:
        """A client with basic auth and a timeout."""
        creds = b64encode(f"{user}:{password}".encode()).decode()
        client = httpx.Client(
            timeout=timeout, headers={"Authorization": f"Basic {creds}"}
        )
        return cls(rpc_url, client=client)

    def close(self) -> None:
        self._client.close()

    def block_at(self, height: int) -> ChainBlock | None:
        """The block at `height` on the node's best chain, or None."""
        try:
            block_hash = self.getblockhash(height)
        except BitcoinNoBlockError:
            return None
        header = self.getblockheader(block_hash)
        return ChainBlock.from_rpc(
            height=header.height,
            block_hash=header.block_hash,
            merkleroot=header.merkle_root,
            confirmations=header.confirmations,
        )

    def getblockchaininfo(self) -> BlockchainInfo:
        result = self._call("getblockchaininfo", [])
        return BlockchainInfo(
            height=int(result["blocks"]),
            best_block_hash=str(result["bestblockhash"]),
            initial_block_download=bool(result["initialblockdownload"]),
            verification_progress=float(result["verificationprogress"]),
        )

    def getblockheader(self, block_hash: str) -> BlockHeader:
        result = self._call("getblockheader", [block_hash, True])
        return BlockHeader(
            block_hash=str(result["hash"]),
            height=int(result["height"]),
            confirmations=int(result["confirmations"]),
            merkle_root=str(result["merkleroot"]),
            time=int(result["time"]),
        )

    def getblockhash(self, height: int) -> str:
        return str(self._call("getblockhash", [height]))

    def _call(self, method: str, params: list[Any]) -> Any:
        for i in range(self._attempts):
            try:
                return self._call_once(method, params)
            except BitcoinRpcTransientError as exc:
                if i == self._attempts - 1:
                    raise
                delay = min(60.0, 2.0 * 2**i)
                logger.warning(
                    "retrying after transient error: %s (sleep=%.3fs, attempt=%d/%d)",
                    exc,
                    delay,
                    i + 1,
                    self._attempts,
                )
                self._sleep(delay)
        raise BitcoinRpcError("unreachable")

    def _raise_rpc_error(self, method: str, error: object) -> None:
        if isinstance(error, Mapping):
            code = cast("Mapping[str, object]", error).get("code")
            if code == -28:
                raise BitcoinRpcTransientError(
                    f"Bitcoin RPC loading: {error}"
                ) from None
            if method == "getblockhash" and code == -8:
                raise BitcoinNoBlockError(
                    f"Bitcoin RPC block missing: {error}"
                ) from None
        raise BitcoinRpcError(f"Bitcoin RPC error: {error}") from None

    def _call_once(self, method: str, params: list[Any]) -> Any:
        payload = {"jsonrpc": "1.0", "id": 0, "method": method, "params": params}
        try:
            response = self._client.post(self._rpc_url, json=payload)
            if not response.is_success:
                with contextlib.suppress(ValueError):
                    body = response.json()
                    if isinstance(body, Mapping):
                        error = cast("Mapping[str, object]", body).get("error")
                        if error is not None:
                            self._raise_rpc_error(method, error)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _scrub_exc_request(exc)
            status = exc.response.status_code
            match status:
                case 401 | 403:
                    raise BitcoinRpcAuthError(
                        f"Bitcoin RPC auth failed: HTTP {status}"
                    ) from exc
                case 429:
                    raise BitcoinRpcTransientError(
                        f"Bitcoin RPC HTTP {status}"
                    ) from exc
                case _ if 500 <= status < 600:
                    raise BitcoinRpcTransientError(
                        f"Bitcoin RPC HTTP {status}"
                    ) from exc
                case _:
                    raise BitcoinRpcError(f"Bitcoin RPC HTTP {status}") from exc
        except httpx.RequestError as exc:
            _scrub_exc_request(exc)
            raise BitcoinRpcTransientError(
                f"Bitcoin RPC transport error: {type(exc).__name__}"
            ) from exc

        body = response.json()
        error = body.get("error")
        if error is not None:
            self._raise_rpc_error(method, error)
        return body["result"]
