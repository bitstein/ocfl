"""Layer-2 tests for ``verify_attestation`` against ``FakeBitcoinRPC``.

API locked in by these tests::

    @dataclass(frozen=True, slots=True)
    class VerifiedAttestation:
        block_height: int
        block_hash: str
        confirmations: int

    @dataclass(frozen=True, slots=True)
    class PendingVerification:
        reason: str  # one of: "pending_attestation",
                     #         "insufficient_confirmations",
                     #         "block_missing"

    VerifyResult = VerifiedAttestation | PendingVerification

    def verify_attestation(
        rpc: BitcoinRPC,
        ots_bytes: bytes,
        expected_digest: bytes,
        *,
        min_confirmations: int = 3,
    ) -> VerifyResult: ...

Raises ``AttestationMismatchError`` if the attested merkle root does not
match the block's merkle root or if the .ots's commitment digest does not
match the caller's ``expected_digest``. Raises ``ReorgDetectedError`` if
the chain tip is shorter than ``block_height`` by more than
``min_confirmations`` blocks.
"""

import hashlib

import pytest
from fakes import FakeBitcoinRPC

from ocfl_cli.errors import (
    AttestationMismatchError,
    ReorgDetectedError,
)
from ocfl_cli.ots.bitcoin import (
    PendingReason,
    PendingVerification,
    VerifiedAttestation,
    verify_attestation,
)
from ocfl_lib import BitcoinNoBlockError
from tests.helpers.ots import build_attestation, build_committed_attestation

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

DIGEST = hashlib.sha256(b"file-bytes").digest()
HEIGHT = 800_000
MIN_CONFS = 3
# bitcoind's RPC returns ``merkleroot`` in display (reversed) hex; the OTS
# attested digest is the raw block-header field in internal byte order. Tests
# register display hex to mirror real bitcoind output.
DIGEST_DISPLAY_HEX = DIGEST[::-1].hex()


def _grow_chain_to(rpc: FakeBitcoinRPC, height: int) -> None:
    """Extend the fake chain so that ``rpc.tip_height >= height``."""
    needed = height - rpc.tip_height
    if needed > 0:
        rpc.extend(needed)


# ---------------------------------------------------------------------------
# success
# ---------------------------------------------------------------------------


class TestSuccess:
    def test_returns_verified_attestation_when_merkle_matches_and_deep(
        self,
    ) -> None:
        ots = build_attestation(DIGEST, block_height=HEIGHT)
        rpc = FakeBitcoinRPC()
        _grow_chain_to(rpc, HEIGHT + MIN_CONFS)
        # Synthetic .ots has no operations between leaf and attestation, so
        # the attested merkle root (internal bytes) equals DIGEST.
        rpc.register_merkle_root(HEIGHT, DIGEST_DISPLAY_HEX)

        result = verify_attestation(rpc, ots, DIGEST, min_confirmations=MIN_CONFS)

        assert isinstance(result, VerifiedAttestation)
        assert result.block_height == HEIGHT
        assert result.block_hash == rpc.getblockhash(HEIGHT)
        assert result.confirmations >= MIN_CONFS


# ---------------------------------------------------------------------------
# AttestationMismatchError
# ---------------------------------------------------------------------------


class TestMismatch:
    def test_wrong_block_merkle_root_raises_attestation_mismatch(self) -> None:
        ots = build_attestation(DIGEST, block_height=HEIGHT)
        rpc = FakeBitcoinRPC()
        _grow_chain_to(rpc, HEIGHT + MIN_CONFS)
        rpc.register_merkle_root(HEIGHT, "00" * 32)  # wrong merkle root

        with pytest.raises(AttestationMismatchError):
            verify_attestation(rpc, ots, DIGEST, min_confirmations=MIN_CONFS)

    def test_commitment_digest_mismatch_raises_attestation_mismatch(
        self,
    ) -> None:
        # .ots commits to DIGEST, but caller passes a different expected digest.
        ots = build_attestation(DIGEST, block_height=HEIGHT)
        rpc = FakeBitcoinRPC()
        _grow_chain_to(rpc, HEIGHT + MIN_CONFS)
        rpc.register_merkle_root(HEIGHT, DIGEST_DISPLAY_HEX)

        wrong_expected = hashlib.sha256(b"some other bytes").digest()
        assert wrong_expected != DIGEST

        with pytest.raises(AttestationMismatchError):
            verify_attestation(rpc, ots, wrong_expected, min_confirmations=MIN_CONFS)


class TestConfirmationsBeforeMerkle:
    def test_insufficient_confs_with_wrong_merkle_is_pending_not_mismatch(
        self,
    ) -> None:
        """A block not yet buried min_confirmations deep is untrusted, so a
        merkle mismatch there must surface as PENDING (it self-heals on a
        shallow fork/reorg), not a HALT-worthy AttestationMismatchError. Only a
        mismatch at trusted depth is a genuine, alarm-worthy divergence.
        """
        ots = build_attestation(DIGEST, block_height=HEIGHT)
        rpc = FakeBitcoinRPC()
        _grow_chain_to(rpc, HEIGHT)  # tip == HEIGHT -> 1 confirmation (< MIN_CONFS)
        rpc.register_merkle_root(HEIGHT, "00" * 32)  # wrong merkle root

        result = verify_attestation(rpc, ots, DIGEST, min_confirmations=MIN_CONFS)

        assert isinstance(result, PendingVerification)
        assert result.reason is PendingReason.INSUFFICIENT_CONFIRMATIONS


class TestAttestedDistinctFromCommitment:
    """With a real merkle path the attested root != the file commitment digest.

    The plain ``build_attestation`` collapses them, so these tests can't tell
    whether ``verify_attestation`` compares the chain merkle against the
    attested digest (correct) or the commitment digest. Here they differ:
    attested = sha256(commitment).
    """

    def test_chain_merkle_matching_attested_verifies(self) -> None:
        ots, attested = build_committed_attestation(DIGEST, block_height=HEIGHT)
        assert attested != DIGEST
        rpc = FakeBitcoinRPC()
        _grow_chain_to(rpc, HEIGHT + MIN_CONFS)
        # Register the ATTESTED root (the value walked through the op), not the
        # commitment digest.
        rpc.register_merkle_root(HEIGHT, attested[::-1].hex())

        result = verify_attestation(rpc, ots, DIGEST, min_confirmations=MIN_CONFS)

        assert isinstance(result, VerifiedAttestation)
        assert result.block_height == HEIGHT

    def test_chain_merkle_matching_commitment_not_attested_raises(self) -> None:
        # Register the chain merkle as the COMMITMENT digest. The commitment
        # check passes (expected == DIGEST), so the only way this raises is the
        # merkle check comparing the chain root against the ATTESTED digest —
        # which it must not satisfy. Kills a mutant that compares the chain
        # root against commitment_digest instead of attested_digest.
        ots, attested = build_committed_attestation(DIGEST, block_height=HEIGHT)
        assert attested != DIGEST
        rpc = FakeBitcoinRPC()
        _grow_chain_to(rpc, HEIGHT + MIN_CONFS)
        rpc.register_merkle_root(HEIGHT, DIGEST_DISPLAY_HEX)

        with pytest.raises(AttestationMismatchError):
            verify_attestation(rpc, ots, DIGEST, min_confirmations=MIN_CONFS)


# ---------------------------------------------------------------------------
# PendingVerification
# ---------------------------------------------------------------------------


class TestPending:
    def test_pending_attestation_only_returns_pending(self) -> None:
        ots = build_attestation(DIGEST, pending_uri="https://a.pool.opentimestamps.org")
        rpc = FakeBitcoinRPC()
        _grow_chain_to(rpc, HEIGHT + MIN_CONFS)

        result = verify_attestation(rpc, ots, DIGEST, min_confirmations=MIN_CONFS)

        assert isinstance(result, PendingVerification)
        assert result.reason == "pending_attestation"

    def test_insufficient_confirmations_returns_pending(self) -> None:
        ots = build_attestation(DIGEST, block_height=HEIGHT)
        rpc = FakeBitcoinRPC()
        _grow_chain_to(rpc, HEIGHT)  # tip == HEIGHT → 1 confirmation
        rpc.register_merkle_root(HEIGHT, DIGEST_DISPLAY_HEX)

        result = verify_attestation(rpc, ots, DIGEST, min_confirmations=MIN_CONFS)

        assert isinstance(result, PendingVerification)
        assert result.reason == "insufficient_confirmations"

    def test_block_missing_within_reorg_window_returns_pending(self) -> None:
        # .ots references HEIGHT, but tip is shallower than HEIGHT — yet
        # within min_confirmations of HEIGHT, so could be a shallow reorg
        # the node hasn't caught up to.
        ots = build_attestation(DIGEST, block_height=HEIGHT)
        rpc = FakeBitcoinRPC()
        _grow_chain_to(rpc, HEIGHT - 1)  # one short of HEIGHT
        # Don't register a merkle root — getblockhash(HEIGHT) will raise.

        result = verify_attestation(rpc, ots, DIGEST, min_confirmations=MIN_CONFS)

        assert isinstance(result, PendingVerification)
        assert result.reason == "block_missing"

    def test_rpc_block_missing_error_within_reorg_window_returns_pending(
        self,
    ) -> None:
        class MissingBlockRPC(FakeBitcoinRPC):
            def getblockhash(self, height: int) -> str:
                raise BitcoinNoBlockError(f"height {height} out of range")

        ots = build_attestation(DIGEST, block_height=HEIGHT)
        rpc = MissingBlockRPC()
        _grow_chain_to(rpc, HEIGHT - 1)

        result = verify_attestation(rpc, ots, DIGEST, min_confirmations=MIN_CONFS)

        assert isinstance(result, PendingVerification)
        assert result.reason == "block_missing"


# ---------------------------------------------------------------------------
# ReorgDetectedError
# ---------------------------------------------------------------------------


class TestReorg:
    def test_chain_far_behind_attestation_raises_reorg_detected(self) -> None:
        ots = build_attestation(DIGEST, block_height=HEIGHT)
        rpc = FakeBitcoinRPC()
        # Chain is HEIGHT - MIN_CONFS - 10 — well past the reorg-tolerance
        # window, indicating a deep reorg or wrong network.
        _grow_chain_to(rpc, HEIGHT - MIN_CONFS - 10)

        with pytest.raises(ReorgDetectedError):
            verify_attestation(rpc, ots, DIGEST, min_confirmations=MIN_CONFS)
