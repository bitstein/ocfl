"""Parse OpenTimestamps proofs and cache attestations."""

import hashlib
import sqlite3
from dataclasses import dataclass

from ocfl_index import db
from ocfl_lib.errors import OtsParseError
from ocfl_lib.ots import parse as parse_ots


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
        parsed = parse_ots(proof_bytes)
    except OtsParseError as exc:
        return ParseFailure(proof_digest=pdigest, error=str(exc))
    return ParsedOts(
        proof_digest=pdigest,
        attested_digest=parsed.commitment_digest.hex(),
        block_height=parsed.block_height,
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
    match result:
        case ParsedOts():
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
        case ParseFailure():
            conn.execute(
                "INSERT INTO ots_attestations(proof_digest, attested_digest, "
                "block_height, parsed_at, parse_error) VALUES (?, '', NULL, ?, ?)",
                (result.proof_digest, now, result.error),
            )
    return pdigest
