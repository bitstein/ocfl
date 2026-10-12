"""Bitcoin for the CLI workflows: the lib's RPC client, ZMQ, ``verify_attestation``.

The RPC client and the attestation check live in ``ocfl_lib`` (ADR-0002).
Here the client raises the CLI's Bitcoin errors, ``block_source`` adapts any
``BitcoinRPC`` to a ``BlockHeaderSource``, and ``verify_attestation`` maps the
lib's check onto the staging workflow's results. ZMQ stays in the CLI.
"""

import contextlib
import time
from base64 import b64encode
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

import httpx2 as httpx
import zmq

from ocfl_cli.errors import (
    AttestationMismatchError,
    BitcoinAuthError,
    BitcoinBlockMissingError,
    BitcoinError,
    BitcoinRPCTransient,
    ReorgDetectedError,
)
from ocfl_lib import (
    BitcoinNoBlockError,
    BitcoinRpcAuthError,
    BitcoinRpcError,
    BitcoinRpcTransientError,
)
from ocfl_lib.bitcoin import BitcoinRpcClient, BlockchainInfo, BlockHeader
from ocfl_lib.ots import (
    AttestationStatus,
    ChainBlock,
    check_attestations,
)
from ocfl_lib.ots import parse as parse_ots

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from ocfl_cli.config import Config
    from ocfl_lib.ots import BlockHeaderSource

__all__ = [
    "BitcoinRPC",
    "BitcoinRPCClient",
    "BitcoinZmqSubscriber",
    "BlockHeader",
    "BlockchainInfo",
    "HashBlockStream",
    "PendingReason",
    "PendingVerification",
    "VerifiedAttestation",
    "VerifyResult",
    "block_source",
    "default_btc_factory",
    "verify_attestation",
]


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


_CLI_ERRORS: tuple[tuple[type[BitcoinRpcError], type[BitcoinError]], ...] = (
    (BitcoinRpcAuthError, BitcoinAuthError),
    (BitcoinNoBlockError, BitcoinBlockMissingError),
    (BitcoinRpcTransientError, BitcoinRPCTransient),
    (BitcoinRpcError, BitcoinError),
)


@contextlib.contextmanager
def _cli_errors() -> Iterator[None]:
    try:
        yield
    except BitcoinRpcError as e:
        cls = next(cli for lib, cli in _CLI_ERRORS if isinstance(e, lib))
        raise cls(str(e)) from e


class BitcoinRPCClient:
    """The lib's ``BitcoinRpcClient``, raising ``ocfl_cli.errors`` Bitcoin errors.

    Callers inject a fully-configured ``httpx2.Client`` (auth + timeout) so
    tests can swap in ``MockTransport``.
    """

    def __init__(
        self,
        rpc_url: str,
        *,
        client: httpx.Client,
        sleep: Callable[[float], None] = time.sleep,
        attempts: int = 3,
    ) -> None:
        self._rpc = BitcoinRpcClient(
            rpc_url, client=client, sleep=sleep, attempts=attempts
        )

    def close(self) -> None:
        self._rpc.close()

    def getblockchaininfo(self) -> BlockchainInfo:
        with _cli_errors():
            return self._rpc.getblockchaininfo()

    def getblockheader(self, block_hash: str) -> BlockHeader:
        with _cli_errors():
            return self._rpc.getblockheader(block_hash)

    def getblockhash(self, height: int) -> str:
        with _cli_errors():
            return self._rpc.getblockhash(height)


class _RpcBlocks:
    def __init__(self, rpc: BitcoinRPC) -> None:
        self._rpc = rpc

    def block_at(self, height: int) -> ChainBlock | None:
        try:
            block_hash = self._rpc.getblockhash(height)
        except BitcoinBlockMissingError:
            return None
        header = self._rpc.getblockheader(block_hash)
        return ChainBlock.from_rpc(
            height=header.height,
            block_hash=header.block_hash,
            merkleroot=header.merkle_root,
            confirmations=header.confirmations,
        )


def block_source(rpc: BitcoinRPC) -> BlockHeaderSource:
    """``rpc`` as a ``BlockHeaderSource``; a missing block is None."""
    return _RpcBlocks(rpc)


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
    """Verify a ``.ots`` against the local chain with the lib's check.

    Raises :class:`AttestationMismatchError` if the proof commits to another
    digest, or if an attestation at a block with ``min_confirmations``
    confirmations does not match its merkle root. A block that is not yet
    that deep is untrusted, so a mismatch there is pending, not an error.
    Raises :class:`ReorgDetectedError` if the local tip is more than
    ``min_confirmations`` blocks behind the lowest attested height.
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

    checks = check_attestations(
        parsed, block_source(rpc), confirmations=min_confirmations
    )
    if any(c.status is AttestationStatus.MISMATCH for c in checks):
        raise AttestationMismatchError(
            "attested merkle root does not match block header"
        )
    for c in checks:
        if c.status is AttestationStatus.VERIFIED and c.block is not None:
            return VerifiedAttestation(
                block_height=c.height,
                block_hash=c.block.block_hash,
                confirmations=c.block.confirmations,
            )
    if checks[0].block is None:
        return PendingVerification(PendingReason.BLOCK_MISSING)
    return PendingVerification(PendingReason.INSUFFICIENT_CONFIRMATIONS)


def default_btc_factory(cfg: Config) -> BitcoinRPC:
    """Construct the production `BitcoinRPCClient` from `cfg`.

    Registered on `ctx.obj["btc_rpc_factory"]` by `main` so service
    callers (and tests via `runner.invoke(obj=...)`) share one entry
    point for client construction.
    """
    btc_cfg = cfg.bitcoin
    if btc_cfg is None:
        raise RuntimeError("bitcoin config missing")

    creds = f"{btc_cfg.rpc_user}:{btc_cfg.rpc_password.get_secret_value()}"
    headers = {"Authorization": "Basic " + b64encode(creds.encode()).decode()}
    client = httpx.Client(timeout=btc_cfg.rpc_timeout_s, headers=headers)
    return BitcoinRPCClient(btc_cfg.rpc_url, client=client)
