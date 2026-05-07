import logging
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

        # Hold the writer lock on a second connection
        other = db.connect(db_path)
        other.execute("BEGIN IMMEDIATE")
        try:
            with (
                caplog.at_level(logging.WARNING, logger=_SCANNER_LOGGER),
                pytest.raises(RefreshInProgress),
            ):
                idx.refresh()
        finally:
            other.rollback()
            other.close()

    # Refresh contention is normal — must not surface as scan-failure warnings
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
            writer.rollback()
            writer.close()
