from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from opentimestamps.core.notary import (
    BitcoinBlockHeaderAttestation,
    PendingAttestation,
)
from opentimestamps.core.op import OpSHA256
from opentimestamps.core.serialize import BytesSerializationContext
from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

from ocfl_index import Index, db, ots
from ocfl_lib import DigestAlgorithm
from tests.conftest import FixtureRoot

if TYPE_CHECKING:
    import sqlite3


def _build_proof(payload: bytes, *, height: int | None = 854_321) -> bytes:
    file_hash = hashlib.sha256(payload).digest()
    ts = Timestamp(file_hash)
    sub = ts.ops.add(OpSHA256())
    if height is None:
        sub.attestations.add(PendingAttestation("https://example.invalid"))
    else:
        sub.attestations.add(BitcoinBlockHeaderAttestation(height))
    dtsf = DetachedTimestampFile(OpSHA256(), ts)
    ctx = BytesSerializationContext()
    dtsf.serialize(ctx)
    return ctx.getbytes()


def _open_db(tmp_path: Path) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "i.db")
    db.migrate(conn, root_path=str(tmp_path))
    return conn


def test_parse_extracts_attested_digest_and_height() -> None:
    proof = _build_proof(b"hello", height=900_000)
    result = ots.parse(proof)
    assert isinstance(result, ots.ParsedOts)
    assert result.attested_digest == hashlib.sha256(b"hello").hexdigest()
    assert result.block_height == 900_000
    assert result.proof_digest == hashlib.sha256(proof).hexdigest()


def test_pending_proof_stores_null_height_no_error(tmp_path: Path) -> None:
    proof = _build_proof(b"pending", height=None)
    conn = _open_db(tmp_path)
    pdigest = ots.upsert_attestation(conn, proof)
    row = conn.execute(
        "SELECT block_height, parse_error FROM ots_attestations WHERE proof_digest = ?",
        (pdigest,),
    ).fetchone()
    assert row == (None, None)


def test_unparseable_stores_parse_error_no_crash(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    pdigest = ots.upsert_attestation(conn, b"\x00garbage-not-an-ots-proof")
    row = conn.execute(
        "SELECT block_height, parse_error FROM ots_attestations WHERE proof_digest = ?",
        (pdigest,),
    ).fetchone()
    assert row[0] is None
    assert row[1]


def test_parse_once_per_proof_digest(tmp_path: Path) -> None:
    proof = _build_proof(b"once", height=800_000)
    conn = _open_db(tmp_path)
    ots.upsert_attestation(conn, proof)

    with patch.object(ots, "parse", wraps=ots.parse) as mock_parse:
        ots.upsert_attestation(conn, proof)
        assert mock_parse.call_count == 0


def test_content_companion_populates_attestations(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    payload = b"hello payload"
    proof = _build_proof(payload, height=850_000)
    fixture.commit(
        "alpha",
        {"data.bin": payload, "data.bin.ots": proof},
        algorithm=DigestAlgorithm.SHA256,
    )

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        idx.refresh()
        rows = idx.conn.execute("SELECT block_height FROM ots_attestations").fetchall()
        assert rows == [(850_000,)]


def test_content_attestation_resolves_sibling_path(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    payload = b"bytes for sibling"
    proof = _build_proof(payload, height=860_000)
    fixture.commit(
        "alpha",
        {"file.txt": payload, "file.txt.ots": proof},
        algorithm=DigestAlgorithm.SHA256,
    )

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        idx.refresh()
        att = idx.content_attestation("alpha", 1, "file.txt")
        assert att is not None
        assert att.block_height == 860_000


def test_duplicate_proof_across_objects_parses_once(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    payload = b"shared bytes"
    proof = _build_proof(payload, height=870_000)
    fixture.commit(
        "alpha",
        {"f.txt": payload, "f.txt.ots": proof},
        algorithm=DigestAlgorithm.SHA256,
    )
    fixture.commit(
        "beta",
        {"f.txt": payload, "f.txt.ots": proof},
        algorithm=DigestAlgorithm.SHA256,
    )

    with patch.object(ots, "parse", wraps=ots.parse) as mock_parse:
        with Index.open(tmp_path / "i.db", fixture.path) as idx:
            idx.refresh()
        assert mock_parse.call_count == 1
