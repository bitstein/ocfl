import sqlite3
from pathlib import Path

import pytest

from ocfl_index import db


class _CodedOperationalError(sqlite3.OperationalError):
    """OperationalError subclass that allows assigning sqlite_errorcode in tests."""

    sqlite_errorcode: int


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def test_ensure_schema_creates_tables_and_seeds_meta(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "i.db")
    db.ensure_schema(conn, root_path="/srv/root")

    tables = _table_names(conn)
    assert {
        "meta",
        "objects",
        "versions",
        "content_paths",
        "blobs",
        "ots_attestations",
        "ots_content",
        "ots_inventory",
    } <= tables

    assert db.meta_get(conn, "schema_version") == str(db.SCHEMA_VERSION)
    assert db.meta_get(conn, "root_path") == "/srv/root"
    assert db.meta_get(conn, "created_at") is not None
    assert db.meta_get(conn, "last_scan_at") is None
    assert db.meta_get(conn, "last_full_scan_at") is None


def test_ensure_schema_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "i.db"
    conn = db.connect(path)
    db.ensure_schema(conn, root_path="/srv/root")
    created = db.meta_get(conn, "created_at")
    conn.close()

    conn2 = db.connect(path)
    db.ensure_schema(conn2, root_path="/srv/root")
    assert db.meta_get(conn2, "created_at") == created


def test_schema_version_mismatch_triggers_rebuild(tmp_path: Path) -> None:
    path = tmp_path / "i.db"
    conn = db.connect(path)
    db.ensure_schema(conn, root_path="/srv/root")
    conn.execute(
        "INSERT INTO objects(object_id, root_relpath, spec_version, "
        "digest_algorithm, head_version, head_version_num, head_created_at, "
        "inventory_digest, indexed_at) VALUES "
        "('o', 'p', '1.1', 'sha256', 'v1', 1, 'now', 'd', 'now')"
    )
    db.meta_set(conn, "schema_version", "0")
    conn.close()

    conn2 = db.connect(path)
    db.ensure_schema(conn2, root_path="/srv/root")
    rows = conn2.execute("SELECT count(*) FROM objects").fetchone()
    assert rows[0] == 0
    assert db.meta_get(conn2, "schema_version") == str(db.SCHEMA_VERSION)


@pytest.mark.parametrize(
    "code",
    [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED],
)
def test_is_lock_error_true_for_lock_errorcodes(code: int) -> None:
    err = _CodedOperationalError("ignored")
    err.sqlite_errorcode = code
    assert db.is_lock_error(err) is True


def test_is_lock_error_false_for_non_lock_errorcode() -> None:
    err = _CodedOperationalError("ignored")
    err.sqlite_errorcode = sqlite3.SQLITE_IOERR
    assert db.is_lock_error(err) is False


@pytest.mark.parametrize(
    "message",
    [
        "database is locked",
        "Database is Locked",
        "database table is locked",
        "database is busy",
    ],
)
def test_is_lock_error_message_fallback_true(message: str) -> None:
    err = sqlite3.OperationalError(message)
    assert db.is_lock_error(err) is True


def test_is_lock_error_message_fallback_false() -> None:
    err = sqlite3.OperationalError("disk I/O error")
    assert db.is_lock_error(err) is False


def _seed_stale_schema(path: Path) -> None:
    conn = db.connect(path)
    db.ensure_schema(conn, root_path="/srv/root")
    conn.execute(
        "INSERT INTO objects(object_id, root_relpath, spec_version, "
        "digest_algorithm, head_version, head_version_num, head_created_at, "
        "inventory_digest, indexed_at) VALUES "
        "('o', 'p', '1.1', 'sha256', 'v1', 1, 'now', 'd', 'now')"
    )
    db.meta_set(conn, "schema_version", "0")
    conn.close()


def test_rebuild_atomic_when_create_fails_before_ddl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "i.db"
    _seed_stale_schema(path)

    def boom(conn: sqlite3.Connection, root_path: str) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(db, "_create_schema_uncommitted", boom)

    conn = db.connect(path)
    with pytest.raises(RuntimeError):
        db.ensure_schema(conn, root_path="/srv/root")

    rows = conn.execute("SELECT count(*) FROM objects").fetchone()
    assert rows[0] == 1
    assert db.meta_get(conn, "schema_version") == "0"
    conn.close()


def test_rebuild_atomic_when_partial_ddl_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "i.db"
    _seed_stale_schema(path)

    def partial(conn: sqlite3.Connection, sql: str) -> None:
        first_stmt, _, _ = sql.partition(";")
        first_stmt = first_stmt.strip()
        if not first_stmt:
            raise sqlite3.OperationalError("empty schema sql")
        conn.execute(first_stmt)
        raise RuntimeError("ddl fail mid-rebuild")

    monkeypatch.setattr(db, "_execute_schema_sql_uncommitted", partial)

    conn = db.connect(path)
    with pytest.raises(RuntimeError):
        db.ensure_schema(conn, root_path="/srv/root")

    rows = conn.execute("SELECT count(*) FROM objects").fetchone()
    assert rows[0] == 1
    assert db.meta_get(conn, "schema_version") == "0"
    conn.close()


def test_standalone_writes_autocommit_cross_connection(tmp_path: Path) -> None:
    path = tmp_path / "i.db"
    conn_a = db.connect(path)
    db.ensure_schema(conn_a, root_path="/srv/root")
    db.meta_set(conn_a, "probe", "value-A")

    conn_b = db.connect(path)
    assert db.meta_get(conn_b, "probe") == "value-A"
    conn_a.close()
    conn_b.close()


def test_immediate_transaction_rejects_nesting(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "i.db")
    db.ensure_schema(conn, root_path="/srv/root")
    # Inner immediate_transaction(conn).__enter__ raises before the body runs;
    # pytest.raises catches it, the outer tx commits cleanly on exit.
    with (
        db.immediate_transaction(conn),
        pytest.raises(RuntimeError, match="nested SQLite transaction"),
        db.immediate_transaction(conn),
    ):
        pass
    conn.close()
