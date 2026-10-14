"""Ports: what the use cases need from the outside world, as Protocols.

Adapters implement them (``ocfl_cli.system``, ``ocfl_cli.ots``, the lib's
``BitcoinRpcClient``); ``main`` wires the production ones into ``AppCtx`` and
tests inject fakes through ``ctx.obj`` under the same names.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from datetime import datetime

    from ocfl_cli.config import Config
    from ocfl_cli.model.simpleproof import (
        JobStatus,
        ResultsEntry,
        SimpleProofDownloadResult,
        SubmitResult,
        VerifyBatchAck,
    )
    from ocfl_lib.bitcoin import BlockchainInfo, BlockHeader
    from ocfl_lib.fs import LocalFS
    from ocfl_lib.fs.s3 import S3FS


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...


@runtime_checkable
class Sleeper(Protocol):
    def sleep(self, seconds: float) -> None: ...


@runtime_checkable
class BlockStream(Protocol):
    def wait_for_block(self, timeout_ms: int) -> bytes | None: ...


class BitcoinRPC(Protocol):
    def getblockchaininfo(self) -> BlockchainInfo: ...

    def getblockheader(self, block_hash: str) -> BlockHeader: ...

    def getblockhash(self, height: int) -> str: ...


class SimpleProofClient(Protocol):
    @property
    def submit_chunk_size(self) -> int: ...

    def submit_hashes(self, digests: list[str], category_id: int) -> SubmitResult: ...

    def check_access(self) -> None: ...

    def verify_batch(self, digests: list[str], category_id: int) -> VerifyBatchAck: ...

    def poll_job(self, job_id: str) -> JobStatus: ...

    def fetch_results_entries(self, results_url: str) -> tuple[ResultsEntry, ...]: ...

    def download_results(
        self, results_url: str, expected_digests: set[str]
    ) -> SimpleProofDownloadResult: ...


type SpClientFactory = Callable[[Config], SimpleProofClient]
type BtcRpcFactory = Callable[[Config], BitcoinRPC]
type BlockStreamFactory = Callable[[Config], BlockStream]
type FsFactory = Callable[[str], LocalFS | S3FS]

DEFAULT_LOCK_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class Runtime:
    clock: Clock
    sleeper: Sleeper
    block_streams: BlockStreamFactory
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS
