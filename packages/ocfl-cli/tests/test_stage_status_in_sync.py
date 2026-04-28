"""Layer-1 unit tests for ``ocfl_cli.commands.stage.compute_in_sync``.

Pins the Bitcoin-in-sync derivation from `OTS workflow contract`
(``stage_status.bitcoin_in_sync``):

    in_sync = (not info.initial_block_download)
              and info.verification_progress >= 0.9999

These are pure unit tests against a constructed ``BlockchainInfo``; they
do NOT touch the CLI surface or the staging directory.

RED until ``compute_in_sync`` is exposed by ``ocfl_cli.commands.stage``.
"""

import pytest

from ocfl_cli.ots.bitcoin import BlockchainInfo


def _info(*, ibd: bool, vp: float) -> BlockchainInfo:
    """Constructor sugar for a ``BlockchainInfo`` exercising the boundary.

    ``height`` and ``best_block_hash`` are irrelevant to ``compute_in_sync``
    so we pin them to fixed sentinels.
    """
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
            # Boundary table from step-17 spec.
            (False, 1.0, True),
            (False, 0.9999, True),
            (False, 0.99989, False),
            (True, 1.0, False),
            (True, 0.5, False),
        ],
    )
    def test_boundary(self, ibd: bool, vp: float, expected: bool) -> None:
        # Imported lazily so collection succeeds even before
        # ``compute_in_sync`` lands; failure at red is a clear
        # ImportError-shaped diagnostic in the test body, not a
        # collection abort.
        from ocfl_cli.stage.service import compute_in_sync

        assert compute_in_sync(_info(ibd=ibd, vp=vp)) is expected
