import sqlite3
from pathlib import Path

from ocfl_index import db


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def test_migrate_creates_tables_and_seeds_meta(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "i.db")
    db.migrate(conn, root_path="/srv/root")

    tables = _table_names(conn)
    assert {
        "meta",
        "objects",
        "versions",
        "content_paths",
        "blobs",
        "ots_attestations",
        "ots_inventory",
    } <= tables

    assert db.meta_get(conn, "schema_version") == str(db.SCHEMA_VERSION)
    assert db.meta_get(conn, "root_path") == "/srv/root"
    assert db.meta_get(conn, "created_at") is not None
    assert db.meta_get(conn, "last_scan_at") is None
    assert db.meta_get(conn, "last_full_scan_at") is None


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "i.db"
    conn = db.connect(path)
    db.migrate(conn, root_path="/srv/root")
    created = db.meta_get(conn, "created_at")
    conn.close()

    conn2 = db.connect(path)
    db.migrate(conn2, root_path="/srv/root")
    assert db.meta_get(conn2, "created_at") == created


def test_schema_version_mismatch_triggers_rebuild(tmp_path: Path) -> None:
    path = tmp_path / "i.db"
    conn = db.connect(path)
    db.migrate(conn, root_path="/srv/root")
    conn.execute(
        "INSERT INTO objects(object_id, root_relpath, spec_version, "
        "digest_algorithm, head_version, head_version_num, head_created_at, "
        "inventory_digest, indexed_at) VALUES "
        "('o', 'p', '1.1', 'sha256', 'v1', 1, 'now', 'd', 'now')"
    )
    db.meta_set(conn, "schema_version", "0")
    conn.commit()
    conn.close()

    conn2 = db.connect(path)
    db.migrate(conn2, root_path="/srv/root")
    rows = conn2.execute("SELECT count(*) FROM objects").fetchone()
    assert rows[0] == 0
    assert db.meta_get(conn2, "schema_version") == str(db.SCHEMA_VERSION)
