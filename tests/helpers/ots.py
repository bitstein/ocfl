"""Synthetic and regtest-anchored ``.ots`` byte builders for tests."""

from typing import TYPE_CHECKING

from bitcoin.core import CBlock
from opentimestamps.bitcoin import make_timestamp_from_block
from opentimestamps.core.notary import (
    BitcoinBlockHeaderAttestation,
    PendingAttestation,
)
from opentimestamps.core.op import OpSHA256
from opentimestamps.core.serialize import BytesSerializationContext
from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

if TYPE_CHECKING:
    from tests.helpers.regtest import RegtestNode


def build_attestation(
    file_digest: bytes,
    *,
    block_height: int | None = None,
    pending_uri: str | None = None,
) -> bytes:
    """Build serialized ``.ots`` bytes; require at least one attestation."""
    if block_height is None and pending_uri is None:
        raise ValueError("must provide block_height or pending_uri")

    timestamp = Timestamp(file_digest)
    if pending_uri is not None:
        timestamp.attestations.add(PendingAttestation(pending_uri))
    if block_height is not None:
        timestamp.attestations.add(BitcoinBlockHeaderAttestation(block_height))

    detached = DetachedTimestampFile(OpSHA256(), timestamp)
    ctx = BytesSerializationContext()
    detached.serialize(ctx)
    return ctx.getbytes()


def build_regtest_attestation(
    node: RegtestNode,
    digest: bytes,
    *,
    wallet: str = "ocfltest",
) -> tuple[bytes, int, str]:
    """Anchor *digest* to a regtest block via ``OP_RETURN``; return real OTS bytes.

    Broadcasts a tx with ``OP_RETURN <digest>``, mines it, then walks the
    block's merkle tree to build a real attestation chain via
    ``opentimestamps.bitcoin.make_timestamp_from_block``.

    Returns ``(ots_bytes, block_height, block_hash)``.
    """
    if len(digest) != 32:
        raise ValueError("digest must be 32 bytes (SHA-256)")

    raw_hex = node.wallet_rpc(
        wallet,
        "createrawtransaction",
        [[], [{"data": digest.hex()}]],
    )
    funded = node.wallet_rpc(wallet, "fundrawtransaction", [raw_hex])
    signed = node.wallet_rpc(wallet, "signrawtransactionwithwallet", [funded["hex"]])
    if not signed.get("complete"):
        raise RuntimeError(f"sign incomplete: {signed.get('errors')}")
    txid = node.wallet_rpc(wallet, "sendrawtransaction", [signed["hex"]])

    addr = node.wallet_rpc(wallet, "getnewaddress")
    block_hash = node.rpc("generatetoaddress", [1, addr])[0]
    header = node.rpc("getblockheader", [block_hash])
    height = int(header["height"])

    block_txids = node.rpc("getblock", [block_hash])["tx"]
    if txid not in block_txids:
        raise RuntimeError(f"tx {txid} not in block {block_hash}")

    block_hex = node.rpc("getblock", [block_hash, 0])
    block = CBlock.deserialize(bytes.fromhex(block_hex))
    timestamp = make_timestamp_from_block(digest, block, height)
    if timestamp is None:
        raise RuntimeError("make_timestamp_from_block could not commit digest")

    detached = DetachedTimestampFile(OpSHA256(), timestamp)
    ctx = BytesSerializationContext()
    detached.serialize(ctx)
    return ctx.getbytes(), height, block_hash
