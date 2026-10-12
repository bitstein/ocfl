"""Pure parser for OpenTimestamps detached timestamp files."""

from dataclasses import dataclass

from opentimestamps.core.notary import BitcoinBlockHeaderAttestation
from opentimestamps.core.serialize import (
    BytesDeserializationContext,
    DeserializationError,
)
from opentimestamps.core.timestamp import DetachedTimestampFile

from ocfl_lib._errors import OtsParseError


@dataclass(frozen=True, slots=True)
class ParsedOts:
    commitment_digest: bytes
    attested_digest: bytes | None
    block_height: int | None


def parse(ots_bytes: bytes) -> ParsedOts:
    """Parse ``.ots`` bytes; raise :class:`OtsParseError` on failure."""
    ctx = BytesDeserializationContext(ots_bytes)
    try:
        detached = DetachedTimestampFile.deserialize(ctx)
    except (DeserializationError, ValueError) as exc:
        raise OtsParseError("failed to deserialize .ots bytes") from exc

    bitcoin_attestations: list[tuple[int, bytes]] = []
    for msg, attestation in detached.timestamp.all_attestations():
        if isinstance(attestation, BitcoinBlockHeaderAttestation):
            bitcoin_attestations.append((attestation.height, msg))

    if bitcoin_attestations:
        block_height, attested_digest = min(bitcoin_attestations, key=lambda x: x[0])
    else:
        block_height = None
        attested_digest = None

    return ParsedOts(
        commitment_digest=detached.timestamp.msg,
        attested_digest=attested_digest,
        block_height=block_height,
    )
