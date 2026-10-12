"""Ports: what the use cases need from the outside world, as Protocols.

Adapters implement them (``ocfl_cli.ots``, the lib's ``BitcoinRpcClient``);
``main`` wires the production ones into ``AppCtx`` and tests inject fakes
through ``ctx.obj`` under the same names.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ocfl_cli.config import Config
    from ocfl_cli.model.simpleproof import (
        JobStatus,
        ResultsEntry,
        SimpleProofDownloadResult,
        SubmitResult,
        VerifyBatchAck,
    )
    from ocfl_lib.bitcoin import BlockchainInfo, BlockHeader


class BitcoinRPC(Protocol):
    def getblockchaininfo(self) -> BlockchainInfo: ...

    def getblockheader(self, block_hash: str) -> BlockHeader: ...

    def getblockhash(self, height: int) -> str: ...


class HashBlockStream(Protocol):
    def wait_for_block(self, timeout_ms: int) -> bytes | None: ...


class SimpleProofClient(Protocol):
    def submit_hashes(self, digests: list[str], category_id: int) -> SubmitResult: ...

    def verify_batch(self, digests: list[str], category_id: int) -> VerifyBatchAck: ...

    def poll_job(self, job_id: str) -> JobStatus: ...

    def fetch_results_entries(self, results_url: str) -> tuple[ResultsEntry, ...]: ...

    def download_results(
        self, results_url: str, expected_digests: set[str]
    ) -> SimpleProofDownloadResult: ...


type SpClientFactory = Callable[[Config], SimpleProofClient]
type BtcRpcFactory = Callable[[Config], BitcoinRPC]
