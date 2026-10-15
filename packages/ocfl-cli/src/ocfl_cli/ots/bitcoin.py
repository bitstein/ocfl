import contextlib
import os
import socket
from base64 import b64encode
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx2 as httpx
import zmq

from ocfl_cli.errors import AttestationMismatchError, ReorgDetectedError
from ocfl_lib import BitcoinNoBlockError
from ocfl_lib.bitcoin import BitcoinRpcClient, BlockchainInfo, BlockHeader
from ocfl_lib.ots import (
    AttestationStatus,
    ChainBlock,
    check_attestations,
)
from ocfl_lib.ots import parse as parse_ots

if TYPE_CHECKING:
    from ocfl_cli.config import Config
    from ocfl_cli.ports import BitcoinRPC, BlockStream
    from ocfl_lib.ots import BlockHeaderSource

__all__ = [
    "BitcoinZmqSubscriber",
    "BlockHeader",
    "BlockchainInfo",
    "PendingReason",
    "PendingVerification",
    "VerifiedAttestation",
    "VerifyResult",
    "block_source",
    "default_block_stream_factory",
    "default_btc_factory",
    "probe_zmq_endpoint",
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


class _RpcBlocks:
    def __init__(self, rpc: BitcoinRPC) -> None:
        self._rpc = rpc

    def block_at(self, height: int) -> ChainBlock | None:
        try:
            block_hash = self._rpc.getblockhash(height)
        except BitcoinNoBlockError:
            return None
        header = self._rpc.getblockheader(block_hash)
        return ChainBlock.from_rpc(
            height=header.height,
            block_hash=header.block_hash,
            merkleroot=header.merkle_root,
            confirmations=header.confirmations,
        )


def block_source(rpc: BitcoinRPC) -> BlockHeaderSource:
    return _RpcBlocks(rpc)


class BitcoinZmqSubscriber:
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
    btc_cfg = cfg.bitcoin
    if btc_cfg is None:
        raise RuntimeError("bitcoin config missing")

    creds = f"{btc_cfg.rpc_user}:{btc_cfg.rpc_password.get_secret_value()}"
    headers = {"Authorization": "Basic " + b64encode(creds.encode()).decode()}
    client = httpx.Client(timeout=btc_cfg.rpc_timeout_s, headers=headers)
    return BitcoinRpcClient(btc_cfg.rpc_url, client=client)


def default_block_stream_factory(cfg: Config) -> BlockStream:
    btc_cfg = cfg.bitcoin
    if btc_cfg is None:
        raise RuntimeError("bitcoin config missing")
    return BitcoinZmqSubscriber(btc_cfg.zmq_endpoint)


def probe_zmq_endpoint(endpoint: str, *, timeout_s: float) -> None:
    """Raise `OSError` unless something listens at a ZMQ endpoint.

    ZMQ connects lazily and has no handshake to wait for, so a `tcp://`
    endpoint is probed with a plain TCP connect and an `ipc://` one by its
    socket path.
    """
    parsed = urlsplit(endpoint)
    if parsed.scheme == "ipc":
        path = endpoint.removeprefix("ipc://")
        if not os.path.exists(path):
            raise FileNotFoundError(f"no ZMQ socket at {path}")
        return
    if parsed.hostname is None or parsed.port is None:
        raise OSError(f"cannot probe ZMQ endpoint {endpoint!r}")
    with socket.create_connection((parsed.hostname, parsed.port), timeout=timeout_s):
        pass
