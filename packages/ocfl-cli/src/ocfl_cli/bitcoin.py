"""Bitcoin RPC + ZMQ client and ``verify_attestation`` helper.

The RPC client is a thin sync ``httpx`` wrapper. Authorization headers are
scrubbed from any propagated ``httpx`` exception so credentials never leak
through tracebacks or log handlers.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import httpx
import zmq

from ocfl_cli.errors import (
    AttestationMismatchError,
    BitcoinAuthError,
    BitcoinError,
    BitcoinRPCTransient,
    ReorgDetectedError,
)
from ocfl_lib.ots import parse as parse_ots

_REDACTED = "<redacted>"
_SENSITIVE_HEADERS: tuple[str, ...] = (
    "authorization",
    "proxy-authorization",
    "cookie",
)


def _scrub_exc_request(exc: BaseException) -> None:
    # httpx stores the request on a private ``_request`` slot; the public
    # ``request`` property raises RuntimeError when unset, so read directly.
    request = getattr(exc, "_request", None)
    if request is None:
        return
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
    block_hash: str
    height: int
    confirmations: int
    merkle_root: str
    time: int


@dataclass(frozen=True, slots=True)
class VerifiedAttestation:
    block_height: int
    block_hash: str
    confirmations: int


class PendingReason(StrEnum):
    PENDING_ATTESTATION = "pending_attestation"
    INSUFFICIENT_CONFIRMATIONS = "insufficient_confirmations"
    BLOCK_MISSING = "block_missing"


@dataclass(frozen=True, slots=True)
class PendingVerification:
    reason: PendingReason


VerifyResult = VerifiedAttestation | PendingVerification


class BitcoinRPC(Protocol):
    def getblockchaininfo(self) -> BlockchainInfo: ...

    def getblockheader(self, block_hash: str) -> BlockHeader: ...

    def getblockhash(self, height: int) -> str: ...


class HashBlockStream(Protocol):
    def wait_for_block(self, timeout_ms: int) -> bytes | None: ...


class BitcoinRPCClient:
    """Sync JSON-RPC client for Bitcoin Core.

    Callers inject a fully-configured ``httpx.Client`` (auth + timeout) so
    construction stays trivial and tests can swap in ``MockTransport``.
    """

    def __init__(self, rpc_url: str, *, client: httpx.Client) -> None:
        self._rpc_url = rpc_url
        self._client = client

    def _call(self, method: str, params: list[Any]) -> Any:
        payload = {
            "jsonrpc": "1.0",
            "id": 0,
            "method": method,
            "params": params,
        }
        try:
            response = self._client.post(self._rpc_url, json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _scrub_exc_request(exc)
            status = exc.response.status_code
            match status:
                case 401 | 403:
                    raise BitcoinAuthError(
                        f"Bitcoin RPC auth failed: HTTP {status}"
                    ) from exc
                case _ if 500 <= status < 600:
                    raise BitcoinRPCTransient(f"Bitcoin RPC HTTP {status}") from exc
                case _:
                    raise BitcoinError(f"Bitcoin RPC HTTP {status}") from exc
        except httpx.RequestError as exc:
            _scrub_exc_request(exc)
            raise BitcoinRPCTransient(
                f"Bitcoin RPC transport error: {type(exc).__name__}"
            ) from exc

        body = response.json()
        if body.get("error") is not None:
            raise BitcoinError(f"Bitcoin RPC error: {body['error']}")
        return body["result"]

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


class BitcoinZmqSubscriber:
    """ZMQ SUB subscriber for Bitcoin Core's ``hashblock`` topic.

    Uses ``zmq.Poller.poll(timeout_ms)`` so Python signal handlers fire
    while waiting for blocks. Tests inject a pre-bound ``socket`` to avoid
    real network sockets.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        ctx: zmq.Context | None = None,
        socket: zmq.Socket | None = None,
    ) -> None:
        if socket is None:
            ctx = ctx if ctx is not None else zmq.Context.instance()
            socket = ctx.socket(zmq.SUB)
            socket.connect(endpoint)
            socket.setsockopt(zmq.SUBSCRIBE, b"hashblock")
        self._socket: zmq.Socket | None = socket
        self._poller = zmq.Poller()
        self._poller.register(socket, zmq.POLLIN)

    def __enter__(self) -> BitcoinZmqSubscriber:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def wait_for_block(self, timeout_ms: int) -> bytes | None:
        socket = self._socket
        if socket is None:
            return None
        events = dict(self._poller.poll(timeout_ms))
        if socket not in events:
            return None
        msg = socket.recv_multipart()
        if len(msg) < 2:
            return None
        return msg[1]

    def close(self) -> None:
        socket = self._socket
        if socket is None:
            return
        with contextlib.suppress(KeyError):
            self._poller.unregister(socket)
        socket.close(linger=0)
        self._socket = None


def verify_attestation(
    rpc: BitcoinRPC,
    ots_bytes: bytes,
    expected_digest: bytes,
    *,
    min_confirmations: int = 3,
) -> VerifyResult:
    """Verify a ``.ots`` against the local chain.

    Raises :class:`AttestationMismatchError` if the parsed commitment digest
    does not match ``expected_digest`` or if the attested merkle root does
    not match the chain's block header. Raises :class:`ReorgDetectedError`
    if the local tip is more than ``min_confirmations`` blocks behind the
    attested height.
    """
    parsed = parse_ots(ots_bytes)

    if parsed.commitment_digest != expected_digest:
        raise AttestationMismatchError(
            "OTS commitment digest does not match expected digest"
        )

    if parsed.block_height is None:
        return PendingVerification(PendingReason.PENDING_ATTESTATION)

    info = rpc.getblockchaininfo()
    if parsed.block_height > info.height + min_confirmations:
        raise ReorgDetectedError(
            f"chain tip {info.height} is more than {min_confirmations} "
            f"blocks behind attested height {parsed.block_height}"
        )

    try:
        block_hash = rpc.getblockhash(parsed.block_height)
    except IndexError:
        return PendingVerification(PendingReason.BLOCK_MISSING)

    header = rpc.getblockheader(block_hash)
    attested = parsed.attested_digest
    # bitcoind's RPC returns ``merkleroot`` as hex of bytes in *reversed*
    # (display) order; OTS Bitcoin attestations commit to the raw block-header
    # field in internal (little-endian) byte order. Reverse before comparing.
    chain_merkle = bytes.fromhex(header.merkle_root)[::-1]
    if attested is None or chain_merkle != attested:
        raise AttestationMismatchError(
            "attested merkle root does not match block header"
        )

    if header.confirmations < min_confirmations:
        return PendingVerification(PendingReason.INSUFFICIENT_CONFIRMATIONS)

    return VerifiedAttestation(
        block_height=parsed.block_height,
        block_hash=block_hash,
        confirmations=header.confirmations,
    )
