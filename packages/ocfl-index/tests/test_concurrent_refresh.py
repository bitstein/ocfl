from pathlib import Path

import pytest

from ocfl_index import Index, RefreshInProgress, db
from ocfl_lib import DigestAlgorithm
from tests.conftest import FixtureRoot


def test_second_refresh_raises_busy_error(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "x"}, algorithm=DigestAlgorithm.SHA256)

    db_path = tmp_path / "i.db"
    with Index.open(db_path, fixture.path) as idx:
        idx.refresh()

        # Hold the writer lock on a second connection
        other = db.connect(db_path)
        other.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(RefreshInProgress):
                idx.refresh()
        finally:
            other.rollback()
            other.close()


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
