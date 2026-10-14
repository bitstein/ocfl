import hashlib
import logging
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from index_fixtures import (
    FixtureRoot,
    build_proof,
    commit_with_inventory_proof,
)

from ocfl_index import Index, db, ots, scanner
from ocfl_lib import DigestAlgorithm, LocalFS

_SCANNER_LOGGER = "ocfl_index.scanner"


def _open_db(tmp_path: Path) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "i.db")
    db.ensure_schema(conn, root_path=str(tmp_path))
    return conn


def test_parse_extracts_attested_digest_and_height() -> None:
    proof = build_proof(b"hello", height=900_000)
    result = ots.parse(proof)
    assert isinstance(result, ots.ParsedOts)
    assert result.attested_digest == hashlib.sha256(b"hello").hexdigest()
    assert result.block_height == 900_000
    assert result.proof_digest == hashlib.sha256(proof).hexdigest()


def test_pending_proof_stores_null_height_no_error(tmp_path: Path) -> None:
    proof = build_proof(b"pending", height=None)
    conn = _open_db(tmp_path)
    result = ots.upsert_attestation(conn, proof)
    assert isinstance(result, ots.ParsedOts)
    row = conn.execute(
        "SELECT block_height, parse_error FROM ots_attestations WHERE proof_digest = ?",
        (result.proof_digest,),
    ).fetchone()
    assert row == (None, None)


def test_unparseable_stores_parse_error_no_crash(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    result = ots.upsert_attestation(conn, b"\x00garbage-not-an-ots-proof")
    assert isinstance(result, ots.ParseFailure)
    assert result.error
    row = conn.execute(
        "SELECT block_height, parse_error FROM ots_attestations WHERE proof_digest = ?",
        (result.proof_digest,),
    ).fetchone()
    assert row[0] is None
    assert row[1]


def test_parse_once_per_proof_digest(tmp_path: Path) -> None:
    proof = build_proof(b"once", height=800_000)
    conn = _open_db(tmp_path)
    first = ots.upsert_attestation(conn, proof)
    assert isinstance(first, ots.ParsedOts)

    with patch.object(ots, "parse", wraps=ots.parse) as mock_parse:
        second = ots.upsert_attestation(conn, proof)
        assert mock_parse.call_count == 0
    assert second == ots.AlreadyIndexed(proof_digest=first.proof_digest)


def test_content_companion_populates_attestations(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    payload = b"hello payload"
    proof = build_proof(payload, height=850_000)
    fixture.commit(
        "alpha",
        {"data.bin": payload, "data.bin.ots": proof},
        algorithm=DigestAlgorithm.SHA256,
    )

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        idx.refresh()
        rows = idx._conn.execute("SELECT block_height FROM ots_attestations").fetchall()
        assert rows == [(850_000,)]


def test_content_attestation_resolves_sibling_path(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    payload = b"bytes for sibling"
    proof = build_proof(payload, height=860_000)
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


def test_content_attestation_resolves_sha512_object(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    payload = b"bytes in sha512 object"
    proof = build_proof(payload, height=861_000)
    fixture.commit(
        "alpha",
        {"file.txt": payload, "file.txt.ots": proof},
        algorithm=DigestAlgorithm.SHA512,
    )

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        idx.refresh()
        proof_content_digest = idx._conn.execute(
            "SELECT digest FROM content_paths WHERE object_id = 'alpha' "
            "AND version_num = 1 AND logical_path = 'file.txt.ots'"
        ).fetchone()[0]
        linked_proof_digest = idx._conn.execute(
            "SELECT proof_digest FROM ots_content WHERE object_id = 'alpha' "
            "AND version_num = 1 AND logical_path = 'file.txt.ots'"
        ).fetchone()[0]
        att = idx.content_attestation("alpha", 1, "file.txt")

    assert proof_content_digest == hashlib.sha512(proof).hexdigest()
    assert linked_proof_digest == hashlib.sha256(proof).hexdigest()
    assert att is not None
    assert att.block_height == 861_000


def test_content_attestation_returns_parse_error_proof(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    bad_proof = b"\x00garbage-not-an-ots-proof"
    fixture.commit(
        "alpha",
        {"file.txt": b"payload", "file.txt.ots": bad_proof},
        algorithm=DigestAlgorithm.SHA512,
    )

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        idx.refresh()
        att = idx.content_attestation("alpha", 1, "file.txt")

    assert att is not None
    assert att.proof_digest == hashlib.sha256(bad_proof).hexdigest()
    assert att.parse_error


def test_inventory_proof_indexed_with_block_height(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    commit_with_inventory_proof(
        fixture, "alpha", {"a.txt": b"x"}, version_num=1, height=880_000
    )

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        idx.refresh()
        row = idx._conn.execute(
            "SELECT a.block_height FROM ots_inventory i "
            "JOIN ots_attestations a ON a.proof_digest = i.proof_digest "
            "WHERE i.object_id = 'alpha' AND i.version_num = 1"
        ).fetchone()
        assert row == (880_000,)


def test_inventory_proof_uses_raw_version_key_for_path(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    conn.execute(
        "INSERT INTO objects(object_id, root_relpath, spec_version, "
        "digest_algorithm, head_version, head_version_num, head_created_at, "
        "inventory_digest, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "alpha",
            "alpha",
            "1.1",
            "sha256",
            "v001",
            1,
            "2024-01-01T00:00:00+00:00",
            "abc123",
            db.now_iso(),
        ),
    )
    conn.execute(
        "INSERT INTO versions(object_id, version_num, version, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("alpha", 1, "v001", "2024-01-01T00:00:00+00:00"),
    )
    proof = build_proof(b"inventory", height=881_000)
    ext_dir = tmp_path / "root" / "alpha" / "extensions" / "NNNN-ots-timestamps"
    ext_dir.mkdir(parents=True)
    (ext_dir / "inventory.json.v001.ots").write_bytes(proof)

    scanner._ingest_ots_inventory_proofs(
        conn,
        LocalFS(tmp_path / "root"),
        relpath="alpha",
        object_id="alpha",
        added_versions=[(1, "v001")],
    )

    row = conn.execute(
        "SELECT a.block_height FROM ots_inventory i "
        "JOIN ots_attestations a ON a.proof_digest = i.proof_digest "
        "WHERE i.object_id = 'alpha' AND i.version_num = 1"
    ).fetchone()
    assert row == (881_000,)


def test_refresh_indexes_zero_padded_inventory_proof_path(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    commit_with_inventory_proof(
        fixture,
        "alpha",
        {"a.txt": b"x"},
        version_num=1,
        version_key="v001",
        height=882_000,
    )

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        idx.refresh()
        att = idx.get_inventory_attestation("alpha", 1)
        version = idx.get_version("alpha", 1)

    assert version is not None
    assert version.version == "v001"
    assert att is not None
    assert att.block_height == 882_000


def test_missing_inventory_proof_is_fine(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "x"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        idx.refresh()
        rows = idx._conn.execute("SELECT count(*) FROM ots_inventory").fetchone()
        assert rows[0] == 0


def test_get_inventory_attestation_returns_row(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    commit_with_inventory_proof(
        fixture, "alpha", {"a.txt": b"x"}, version_num=1, height=890_000
    )

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        idx.refresh()
        att = idx.get_inventory_attestation("alpha", 1)
        assert att is not None
        assert att.block_height == 890_000


def test_first_version_attestation_sugar(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    commit_with_inventory_proof(
        fixture, "alpha", {"a.txt": b"x"}, version_num=1, height=900_000
    )

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        idx.refresh()
        att = idx.first_version_attestation("alpha")
        assert att is not None
        assert att.block_height == 900_000


def test_unparseable_content_proof_warns_once_per_proof_digest(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fixture = FixtureRoot(tmp_path)
    bad_proof = b"\x00garbage-not-an-ots-proof"
    fixture.commit(
        "alpha",
        {"file.txt": b"payload", "file.txt.ots": bad_proof},
        algorithm=DigestAlgorithm.SHA256,
    )
    expected_pdigest = hashlib.sha256(bad_proof).hexdigest()

    with (
        Index.open(tmp_path / "i.db", fixture.path) as idx,
        caplog.at_level(logging.WARNING, logger=_SCANNER_LOGGER),
    ):
        idx.refresh()
        idx.refresh()

    records = [
        r
        for r in caplog.records
        if r.name == _SCANNER_LOGGER and "ots proof parse failed" in r.message
    ]
    assert len(records) == 1, [r.message for r in records]
    rec = records[0]
    assert f"proof_digest={expected_pdigest}" in rec.message
    assert "error=" in rec.message
    assert rec.__dict__["object_id"] == "alpha"
    assert rec.__dict__["version_num"] == 1
    assert rec.__dict__["logical_path"] == "file.txt.ots"
    assert rec.__dict__["proof_digest"] == expected_pdigest


def test_unparseable_inventory_proof_warns_with_proof_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    conn = _open_db(tmp_path)
    conn.execute(
        "INSERT INTO objects(object_id, root_relpath, spec_version, "
        "digest_algorithm, head_version, head_version_num, head_created_at, "
        "inventory_digest, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "alpha",
            "alpha",
            "1.1",
            "sha256",
            "v001",
            1,
            "2024-01-01T00:00:00+00:00",
            "abc123",
            db.now_iso(),
        ),
    )
    conn.execute(
        "INSERT INTO versions(object_id, version_num, version, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("alpha", 1, "v001", "2024-01-01T00:00:00+00:00"),
    )
    bad_proof = b"\x00garbage-not-an-ots-proof"
    expected_pdigest = hashlib.sha256(bad_proof).hexdigest()
    ext_dir = tmp_path / "root" / "alpha" / "extensions" / "NNNN-ots-timestamps"
    ext_dir.mkdir(parents=True)
    proof_path = ext_dir / "inventory.json.v001.ots"
    proof_path.write_bytes(bad_proof)
    expected_relpath = "alpha/extensions/NNNN-ots-timestamps/inventory.json.v001.ots"

    with caplog.at_level(logging.WARNING, logger=_SCANNER_LOGGER):
        scanner._ingest_ots_inventory_proofs(
            conn,
            LocalFS(tmp_path / "root"),
            relpath="alpha",
            object_id="alpha",
            added_versions=[(1, "v001")],
        )

    records = [
        r
        for r in caplog.records
        if r.name == _SCANNER_LOGGER and "ots inventory proof parse failed" in r.message
    ]
    assert len(records) == 1, [r.message for r in records]
    rec = records[0]
    assert f"proof_path={expected_relpath}" in rec.message
    assert f"proof_digest={expected_pdigest}" in rec.message
    assert "error=" in rec.message
    assert rec.__dict__["object_id"] == "alpha"
    assert rec.__dict__["version_num"] == 1
    assert rec.__dict__["proof_path"] == expected_relpath
    assert rec.__dict__["proof_digest"] == expected_pdigest


def test_duplicate_proof_across_objects_parses_once(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    payload = b"shared bytes"
    proof = build_proof(payload, height=870_000)
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
            link_count = idx._conn.execute(
                "SELECT count(*) FROM ots_content"
            ).fetchone()[0]
        assert mock_parse.call_count == 1
        assert link_count == 2
