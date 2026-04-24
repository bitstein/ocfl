"""Bitcoin RPC + ZMQ protocols and skeleton implementations."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


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


@runtime_checkable
class BitcoinRPC(Protocol):
    def getblockchaininfo(self) -> BlockchainInfo: ...

    def getblockheader(self, block_hash: str) -> BlockHeader: ...

    def getblockhash(self, height: int) -> str: ...

    def verify_attestation(
        self, ots_bytes: bytes, expected_digest: str
    ) -> BlockHeader: ...


@runtime_checkable
class HashBlockStream(Protocol):
    def wait_for_block(self, timeout_ms: int) -> bytes | None: ...


class BitcoinRPCClient:
    """Real RPC implementation."""

    def getblockchaininfo(self) -> BlockchainInfo:
        raise NotImplementedError

    def getblockheader(self, block_hash: str) -> BlockHeader:
        raise NotImplementedError

    def getblockhash(self, height: int) -> str:
        raise NotImplementedError

    def verify_attestation(self, ots_bytes: bytes, expected_digest: str) -> BlockHeader:
        raise NotImplementedError


class BitcoinZmqSubscriber:
    """Real ZMQ hashblock subscriber."""

    def wait_for_block(self, timeout_ms: int) -> bytes | None:
        raise NotImplementedError
