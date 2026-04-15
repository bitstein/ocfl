from pathlib import Path

import pytest

from ocfl_index import Index, IndexRootMismatch, db
from tests.conftest import FixtureRoot


def test_open_creates_db_and_seeds_root(
    tmp_path: Path, fixture_root: FixtureRoot
) -> None:
    db_path = tmp_path / "i.db"
    with Index.open(db_path, fixture_root.path) as idx:
        assert db_path.exists()
        assert idx.root_path == fixture_root.path
        assert idx.storage_root is not None


def test_reopen_with_mismatched_root_raises(
    tmp_path: Path, fixture_root: FixtureRoot
) -> None:
    db_path = tmp_path / "i.db"
    Index.open(db_path, fixture_root.path).close()

    other = FixtureRoot(tmp_path / "other_base")
    with pytest.raises(IndexRootMismatch):
        Index.open(db_path, other.path)


def test_reopen_with_stale_schema_rebuilds(
    tmp_path: Path, fixture_root: FixtureRoot
) -> None:
    db_path = tmp_path / "i.db"
    idx = Index.open(db_path, fixture_root.path)
    idx.conn.execute(
        "INSERT INTO objects(object_id, root_relpath, spec_version, "
        "digest_algorithm, head_version, head_version_num, head_created_at, "
        "inventory_digest, indexed_at) VALUES "
        "('o', 'p', '1.1', 'sha256', 'v1', 1, 'now', 'd', 'now')"
    )
    db.meta_set(idx.conn, "schema_version", "0")
    idx.conn.commit()
    idx.close()

    with Index.open(db_path, fixture_root.path) as idx2:
        rows = idx2.conn.execute("SELECT count(*) FROM objects").fetchone()
        assert rows[0] == 0
        assert db.meta_get(idx2.conn, "schema_version") == str(db.SCHEMA_VERSION)
