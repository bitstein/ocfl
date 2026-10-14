import hashlib
import sqlite3
from dataclasses import dataclass

from ocfl_index import db
from ocfl_lib import OtsParseError
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


@dataclass(frozen=True, slots=True)
class UpsertAttestationResult:
    """Outcome of a single ``upsert_attestation`` call.

    ``parse_error`` and ``was_inserted`` describe what happened *in this call*,
    not the cached row's full state. A cached-row hit returns
    ``parse_error=None, was_inserted=False`` regardless of historical parse
    state — callers needing the stored error should query
    ``ots_attestations`` directly.
    """

    proof_digest: str
    parse_error: str | None
    was_inserted: bool


def proof_digest(proof_bytes: bytes) -> str:
    return hashlib.sha256(proof_bytes).hexdigest()


def parse(proof_bytes: bytes) -> ParsedOts | ParseFailure:
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


def upsert_attestation(
    conn: sqlite3.Connection, proof_bytes: bytes
) -> UpsertAttestationResult:
    """Parse and upsert. Idempotent on proof_digest.

    Returns an :class:`UpsertAttestationResult` describing this call. Cached
    hits return ``was_inserted=False`` and ``parse_error=None`` so callers can
    log "first time this proof was indexed" events without re-firing on every
    refresh.
    """
    pdigest = proof_digest(proof_bytes)
    existing = conn.execute(
        "SELECT 1 FROM ots_attestations WHERE proof_digest = ?",
        (pdigest,),
    ).fetchone()
    if existing is not None:
        return UpsertAttestationResult(
            proof_digest=pdigest, parse_error=None, was_inserted=False
        )
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
            return UpsertAttestationResult(
                proof_digest=pdigest, parse_error=None, was_inserted=True
            )
        case ParseFailure():
            conn.execute(
                "INSERT INTO ots_attestations(proof_digest, attested_digest, "
                "block_height, parsed_at, parse_error) VALUES (?, '', NULL, ?, ?)",
                (result.proof_digest, now, result.error),
            )
            return UpsertAttestationResult(
                proof_digest=pdigest,
                parse_error=result.error,
                was_inserted=True,
            )
