import logging
import sqlite3
from pathlib import Path

import pytest
from index_fixtures import FixtureRoot

from ocfl_index import Index, RefreshInProgress, db
from ocfl_lib import DigestAlgorithm

_SCANNER_LOGGER = "ocfl_index.scanner"


def test_second_refresh_raises_busy_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "x"}, algorithm=DigestAlgorithm.SHA256)

    db_path = tmp_path / "i.db"
    with Index.open(db_path, fixture.path) as idx:
        idx.refresh()

        other = db.connect(db_path)
        other.execute("BEGIN IMMEDIATE")
        try:
            with (
                caplog.at_level(logging.WARNING, logger=_SCANNER_LOGGER),
                pytest.raises(RefreshInProgress),
            ):
                idx.refresh()
        finally:
            other.execute("ROLLBACK")
            other.close()

    scan_warnings = [
        r
        for r in caplog.records
        if r.name == _SCANNER_LOGGER and "scan failed" in r.message
    ]
    assert scan_warnings == []


def test_reader_not_blocked_during_refresh(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "x"}, algorithm=DigestAlgorithm.SHA256)

    db_path = tmp_path / "i.db"
    with Index.open(db_path, fixture.path) as idx:
        idx.refresh()

        writer = db.connect(db_path)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE meta SET value = 'x' WHERE key = 'root_path'")
        try:
            reader = db.connect(db_path)
            rows = reader.execute("SELECT object_id FROM objects").fetchall()
            assert rows == [("alpha",)]
            reader.close()
        finally:
            writer.execute("ROLLBACK")
            writer.close()


def test_open_with_stale_schema_under_writer_lock_raises_refresh_in_progress(
    tmp_path: Path,
) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "x"}, algorithm=DigestAlgorithm.SHA256)

    db_path = tmp_path / "i.db"
    Index.open(db_path, fixture.path).close()

    side = db.connect(db_path)
    db.meta_set(side, "schema_version", "0")
    side.close()

    holder = db.connect(db_path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(RefreshInProgress):
            Index.open(db_path)
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_open_with_clean_schema_under_writer_lock_succeeds(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "x"}, algorithm=DigestAlgorithm.SHA256)

    db_path = tmp_path / "i.db"
    Index.open(db_path, fixture.path).close()

    holder = db.connect(db_path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with Index.open(db_path) as idx:
            assert idx.root_path == fixture.path
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_open_non_lock_operational_error_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "x"}, algorithm=DigestAlgorithm.SHA256)

    db_path = tmp_path / "i.db"
    Index.open(db_path, fixture.path).close()

    def boom(_conn: sqlite3.Connection, root_path: str) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr("ocfl_index.index.db.ensure_schema", boom)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        Index.open(db_path)


def test_open_closes_connection_on_non_lock_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "x"}, algorithm=DigestAlgorithm.SHA256)

    db_path = tmp_path / "i.db"
    Index.open(db_path, fixture.path).close()

    captured: list[sqlite3.Connection] = []
    real_connect = db.connect

    def spy(path: Path | str) -> sqlite3.Connection:
        conn = real_connect(path)
        captured.append(conn)
        return conn

    def boom(_conn: sqlite3.Connection, root_path: str) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr("ocfl_index.index.db.connect", spy)
    monkeypatch.setattr("ocfl_index.index.db.ensure_schema", boom)
    with pytest.raises(sqlite3.OperationalError):
        Index.open(db_path)

    assert len(captured) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        captured[0].execute("SELECT 1")
