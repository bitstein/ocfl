from collections import deque
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Self

from ocfl_cli.ots.bitcoin import BlockchainInfo, BlockHeader
from ocfl_lib import BitcoinNoBlockError


def _hash_at(height: int, salt: bytes = b"") -> str:
    return sha256(f"block:{height}".encode() + salt).hexdigest()


def _merkle_at(height: int, block_hash: str) -> str:
    return sha256(f"merkle:{height}:{block_hash}".encode()).hexdigest()


@dataclass
class FakeBitcoinRPC:
    _chain: list[str] = field(default_factory=list)
    _salt: bytes = b""
    _merkle_roots: dict[int, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self._chain:
            self._chain.append(_hash_at(0, self._salt))

    @property
    def tip_height(self) -> int:
        return len(self._chain) - 1

    def extend(self, n: int) -> None:
        for _ in range(n):
            self._chain.append(_hash_at(len(self._chain), self._salt))

    def reorg_at(self, height: int) -> None:
        if height < 0 or height > self.tip_height:
            raise ValueError(f"height {height} outside chain")
        self._salt = self._salt + b"r"
        del self._chain[height:]
        self._chain.append(_hash_at(height, self._salt))
        self._forget_merkle_roots_from(height)

    def _forget_merkle_roots_from(self, height: int) -> None:
        for h in [h for h in self._merkle_roots if h >= height]:
            del self._merkle_roots[h]

    def register_merkle_root(self, height: int, merkle_hex: str) -> None:
        """Register the merkle_root reported by getblockheader at *height*.

        ``merkle_hex`` follows bitcoind's RPC convention: hex of the merkle
        root bytes in *reversed* (display) order.
        """
        self._merkle_roots[height] = merkle_hex

    def getblockchaininfo(self) -> BlockchainInfo:
        return BlockchainInfo(
            height=self.tip_height,
            best_block_hash=self._chain[-1],
            initial_block_download=False,
            verification_progress=1.0,
        )

    def getblockheader(self, block_hash: str) -> BlockHeader:
        height = self._chain.index(block_hash)
        return BlockHeader(
            block_hash=block_hash,
            height=height,
            confirmations=self.tip_height - height + 1,
            merkle_root=self._merkle_roots.get(height, _merkle_at(height, block_hash)),
            time=0,
        )

    def getblockhash(self, height: int) -> str:
        if height < 0 or height > self.tip_height:
            raise BitcoinNoBlockError(f"height {height} out of range")
        return self._chain[height]


class FakeBlockStream:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


@dataclass
class InMemoryHashBlockStream(FakeBlockStream):
    _feed: deque[bytes] = field(default_factory=deque)

    def push(self, block_hash: bytes) -> None:
        self._feed.append(block_hash)

    def wait_for_block(self, timeout_ms: int) -> bytes | None:
        if not self._feed:
            return None
        return self._feed.popleft()
