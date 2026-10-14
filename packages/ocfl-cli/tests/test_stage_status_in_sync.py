import pytest

from ocfl_cli.ots.bitcoin import BlockchainInfo
from ocfl_cli.workflow.status import compute_in_sync


def _info(*, ibd: bool, vp: float) -> BlockchainInfo:
    return BlockchainInfo(
        height=0,
        best_block_hash="0" * 64,
        initial_block_download=ibd,
        verification_progress=vp,
    )


class TestComputeInSyncBoundary:
    @pytest.mark.parametrize(
        ("ibd", "vp", "expected"),
        [
            (False, 1.0, True),
            (False, 0.9999, True),
            (False, 0.99989, False),
            (True, 1.0, False),
            (True, 0.5, False),
        ],
    )
    def test_boundary(self, ibd: bool, vp: float, expected: bool) -> None:
        assert compute_in_sync(_info(ibd=ibd, vp=vp)) is expected
