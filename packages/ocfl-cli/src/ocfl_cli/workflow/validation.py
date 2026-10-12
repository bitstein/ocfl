"""Validation use cases: the Bitcoin checks of the OTS extension."""

from typing import TYPE_CHECKING

from ocfl_cli.ots.bitcoin import block_source

if TYPE_CHECKING:
    from ocfl_cli.ports import BitcoinRPC
    from ocfl_lib import ots


def chain_source(rpc: BitcoinRPC) -> ots.BlockHeaderSource:
    """`rpc` as a block-header source that asks the node once per height."""
    return _CachedBlocks(block_source(rpc))


class _CachedBlocks:
    """Asks the node once per height, so progress output costs no extra calls."""

    def __init__(self, source: ots.BlockHeaderSource) -> None:
        self._source = source
        self._blocks: dict[int, ots.ChainBlock | None] = {}

    def block_at(self, height: int) -> ots.ChainBlock | None:
        if height not in self._blocks:
            self._blocks[height] = self._source.block_at(height)
        return self._blocks[height]
