"""Parse OpenTimestamps proofs and cache attestations."""

import hashlib
import sqlite3
from dataclasses import dataclass

from opentimestamps.core.notary import BitcoinBlockHeaderAttestation
from opentimestamps.core.serialize import BytesDeserializationContext
from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

from ocfl_index import db


@dataclass(frozen=True, slots=True)
class ParsedOts:
    proof_digest: str
    attested_digest: str
    block_height: int | None


@dataclass(frozen=True, slots=True)
class ParseFailure:
    proof_digest: str
    error: str


def proof_digest(proof_bytes: bytes) -> str:
    return hashlib.sha256(proof_bytes).hexdigest()


def parse(proof_bytes: bytes) -> ParsedOts | ParseFailure:
    """Parse OTS bytes. Returns a ParsedOts (height may be None) or ParseFailure."""
    pdigest = proof_digest(proof_bytes)
    try:
        ctx = BytesDeserializationContext(proof_bytes)
        dtsf = DetachedTimestampFile.deserialize(ctx)
    except Exception as exc:
        return ParseFailure(proof_digest=pdigest, error=str(exc))
    return ParsedOts(
        proof_digest=pdigest,
        attested_digest=dtsf.file_digest.hex(),
        block_height=_first_btc_height(dtsf.timestamp),
    )


def upsert_attestation(conn: sqlite3.Connection, proof_bytes: bytes) -> str:
    """Parse and upsert. Idempotent on proof_digest. Returns the digest."""
    pdigest = proof_digest(proof_bytes)
    existing = conn.execute(
        "SELECT 1 FROM ots_attestations WHERE proof_digest = ?",
        (pdigest,),
    ).fetchone()
    if existing is not None:
        return pdigest
    result = parse(proof_bytes)
    now = db.now_iso()
    if isinstance(result, ParsedOts):
        conn.execute(
            "INSERT INTO ots_attestations(proof_digest, attested_digest, "
            "block_height, parsed_at, parse_error) VALUES (?, ?, ?, ?, NULL)",
            (
                result.proof_digest,
                result.attested_digest,
                result.block_height,
                now,
            ),
        )
    else:
        conn.execute(
            "INSERT INTO ots_attestations(proof_digest, attested_digest, "
            "block_height, parsed_at, parse_error) VALUES (?, '', NULL, ?, ?)",
            (result.proof_digest, now, result.error),
        )
    return pdigest


def _first_btc_height(timestamp: Timestamp) -> int | None:
    """Return the smallest Bitcoin block height across the proof tree, or None."""
    heights: list[int] = []
    _collect_heights(timestamp, heights)
    return min(heights) if heights else None


def _collect_heights(timestamp: Timestamp, out: list[int]) -> None:
    for att in timestamp.attestations:
        if isinstance(att, BitcoinBlockHeaderAttestation):
            out.append(att.height)
    for sub in timestamp.ops.values():
        _collect_heights(sub, out)
