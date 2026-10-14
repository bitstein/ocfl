import hashlib

import pytest
from fakes import FakeBitcoinRPC
from ots_helpers import (
    DIGEST_BLOCK_HEIGHT,
    MIN_CONFS,
    bury,
    end_chain_below,
    register_mismatched_merkle_root,
)

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
from tests.helpers.ots import build_attestation

DIGEST = hashlib.sha256(b"file-bytes").digest()
# bitcoind's RPC returns ``merkleroot`` in display (reversed) hex; the OTS
# attested digest is the raw block-header field in internal byte order. Tests
# register display hex to mirror real bitcoind output.
DIGEST_DISPLAY_HEX = DIGEST[::-1].hex()


class TestSuccess:
    def test_returns_verified_attestation_when_merkle_matches_and_deep(
        self,
    ) -> None:
        ots = build_attestation(DIGEST, block_height=DIGEST_BLOCK_HEIGHT)
        rpc = FakeBitcoinRPC()
        bury(rpc)
        rpc.register_merkle_root(DIGEST_BLOCK_HEIGHT, DIGEST_DISPLAY_HEX)

        result = verify_attestation(rpc, ots, DIGEST, min_confirmations=MIN_CONFS)

        assert isinstance(result, VerifiedAttestation)
        assert result.block_height == DIGEST_BLOCK_HEIGHT
        assert result.block_hash == rpc.getblockhash(DIGEST_BLOCK_HEIGHT)
        assert result.confirmations >= MIN_CONFS


class TestMismatch:
    def test_wrong_block_merkle_root_raises_attestation_mismatch(self) -> None:
        ots = build_attestation(DIGEST, block_height=DIGEST_BLOCK_HEIGHT)
        rpc = FakeBitcoinRPC()
        bury(rpc)
        register_mismatched_merkle_root(rpc)

        with pytest.raises(AttestationMismatchError):
            verify_attestation(rpc, ots, DIGEST, min_confirmations=MIN_CONFS)

    def test_commitment_digest_mismatch_raises_attestation_mismatch(
        self,
    ) -> None:
        ots = build_attestation(DIGEST, block_height=DIGEST_BLOCK_HEIGHT)
        rpc = FakeBitcoinRPC()
        bury(rpc)
        rpc.register_merkle_root(DIGEST_BLOCK_HEIGHT, DIGEST_DISPLAY_HEX)

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
        ots = build_attestation(DIGEST, block_height=DIGEST_BLOCK_HEIGHT)
        rpc = FakeBitcoinRPC()
        bury(rpc, 1)
        register_mismatched_merkle_root(rpc)

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
        ots = build_attestation(
            DIGEST, block_height=DIGEST_BLOCK_HEIGHT, merkle_step=True
        )
        attested = hashlib.sha256(DIGEST).digest()
        assert attested != DIGEST
        rpc = FakeBitcoinRPC()
        bury(rpc)
        rpc.register_merkle_root(DIGEST_BLOCK_HEIGHT, attested[::-1].hex())

        result = verify_attestation(rpc, ots, DIGEST, min_confirmations=MIN_CONFS)

        assert isinstance(result, VerifiedAttestation)
        assert result.block_height == DIGEST_BLOCK_HEIGHT

    def test_chain_merkle_matching_commitment_not_attested_raises(self) -> None:
        ots = build_attestation(
            DIGEST, block_height=DIGEST_BLOCK_HEIGHT, merkle_step=True
        )
        attested = hashlib.sha256(DIGEST).digest()
        assert attested != DIGEST
        rpc = FakeBitcoinRPC()
        bury(rpc)
        rpc.register_merkle_root(DIGEST_BLOCK_HEIGHT, DIGEST_DISPLAY_HEX)

        with pytest.raises(AttestationMismatchError):
            verify_attestation(rpc, ots, DIGEST, min_confirmations=MIN_CONFS)


class TestPending:
    def test_pending_attestation_only_returns_pending(self) -> None:
        ots = build_attestation(DIGEST, pending_uri="https://a.pool.opentimestamps.org")
        rpc = FakeBitcoinRPC()
        bury(rpc)

        result = verify_attestation(rpc, ots, DIGEST, min_confirmations=MIN_CONFS)

        assert isinstance(result, PendingVerification)
        assert result.reason == "pending_attestation"

    def test_insufficient_confirmations_returns_pending(self) -> None:
        ots = build_attestation(DIGEST, block_height=DIGEST_BLOCK_HEIGHT)
        rpc = FakeBitcoinRPC()
        bury(rpc, 1)
        rpc.register_merkle_root(DIGEST_BLOCK_HEIGHT, DIGEST_DISPLAY_HEX)

        result = verify_attestation(rpc, ots, DIGEST, min_confirmations=MIN_CONFS)

        assert isinstance(result, PendingVerification)
        assert result.reason == "insufficient_confirmations"

    def test_block_missing_within_reorg_window_returns_pending(self) -> None:
        ots = build_attestation(DIGEST, block_height=DIGEST_BLOCK_HEIGHT)
        rpc = FakeBitcoinRPC()
        end_chain_below(rpc, 1)

        result = verify_attestation(rpc, ots, DIGEST, min_confirmations=MIN_CONFS)

        assert isinstance(result, PendingVerification)
        assert result.reason == "block_missing"

    def test_rpc_block_missing_error_within_reorg_window_returns_pending(
        self,
    ) -> None:
        class MissingBlockRPC(FakeBitcoinRPC):
            def getblockhash(self, height: int) -> str:
                raise BitcoinNoBlockError(f"height {height} out of range")

        ots = build_attestation(DIGEST, block_height=DIGEST_BLOCK_HEIGHT)
        rpc = MissingBlockRPC()
        end_chain_below(rpc, 1)

        result = verify_attestation(rpc, ots, DIGEST, min_confirmations=MIN_CONFS)

        assert isinstance(result, PendingVerification)
        assert result.reason == "block_missing"


class TestReorg:
    def test_chain_far_behind_attestation_raises_reorg_detected(self) -> None:
        ots = build_attestation(DIGEST, block_height=DIGEST_BLOCK_HEIGHT)
        rpc = FakeBitcoinRPC()
        end_chain_below(rpc, MIN_CONFS + 10)

        with pytest.raises(ReorgDetectedError):
            verify_attestation(rpc, ots, DIGEST, min_confirmations=MIN_CONFS)
