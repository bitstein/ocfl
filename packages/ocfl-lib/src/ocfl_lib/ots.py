"""Pure parser for OpenTimestamps detached timestamp files."""

from dataclasses import dataclass

from opentimestamps.core.notary import BitcoinBlockHeaderAttestation
from opentimestamps.core.serialize import (
    BytesDeserializationContext,
    DeserializationError,
)
from opentimestamps.core.timestamp import DetachedTimestampFile

from ocfl_lib.errors import OtsParseError


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

    attested_digest: bytes | None = None
    block_height: int | None = None
    for msg, attestation in detached.timestamp.all_attestations():
        if isinstance(attestation, BitcoinBlockHeaderAttestation):
            attested_digest = msg
            block_height = attestation.height
            break

    return ParsedOts(
        commitment_digest=detached.timestamp.msg,
        attested_digest=attested_digest,
        block_height=block_height,
    )
