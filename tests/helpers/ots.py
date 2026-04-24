"""Synthetic ``.ots`` byte builder for tests."""

from opentimestamps.core.notary import (
    BitcoinBlockHeaderAttestation,
    PendingAttestation,
)
from opentimestamps.core.op import OpSHA256
from opentimestamps.core.serialize import BytesSerializationContext
from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp


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
