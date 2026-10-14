import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import NewType

SCHEMA_VERSION = 1

Tx = NewType("Tx", sqlite3.Connection)

_TABLES = (
    "ots_inventory",
    "ots_content",
    "ots_attestations",
    "blobs",
    "content_paths",
    "versions",
    "objects",
    "meta",
)

_CACHE_TABLES = (
    "ots_inventory",
    "ots_content",
    "ots_attestations",
    "blobs",
    "content_paths",
    "versions",
    "objects",
)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


_LOCK_MESSAGE_FRAGMENTS = (
    "database is locked",
    "database table is locked",
    "database is busy",
)


def is_lock_error(err: sqlite3.OperationalError) -> bool:
    code = getattr(err, "sqlite_errorcode", None)
    if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return True
    msg = str(err).lower()
    return any(fragment in msg for fragment in _LOCK_MESSAGE_FRAGMENTS)


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=0, autocommit=True)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 0")
    return conn


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Generator[Tx]:
    """Run a multi-statement write inside a single ``BEGIN IMMEDIATE`` tx.

    The only sanctioned grouping primitive outside ``scanner.scan``. Rejects
    nested calls so accidental nesting fails loudly at the call site instead of
    silently sharing a tx. Catches ``BaseException`` so interrupt-style exits
    still roll back. Also rolls back if ``COMMIT`` itself raises (e.g. deferred
    FK violation), so the writer lock is never held past helper exit.
    """
    if conn.in_transaction:
        raise RuntimeError("nested SQLite transaction")
    # Under autocommit=True, Connection.commit/rollback are no-ops, so we
    # drive the explicit transaction with raw SQL.
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield Tx(conn)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def ensure_schema(conn: sqlite3.Connection, root_path: str) -> None:
    stored = _stored_schema_version(conn)
    if stored is None:
        with immediate_transaction(conn) as tx:
            _create_schema(tx, root_path)
    elif stored != SCHEMA_VERSION:
        with immediate_transaction(conn) as tx:
            _drop_schema(tx)
            _create_schema(tx, root_path)


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def reset_cache(conn: sqlite3.Connection) -> None:
    for table in _CACHE_TABLES:
        conn.execute(f"DELETE FROM {table}")


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _stored_schema_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    if row is None:
        return None
    raw = meta_get(conn, "schema_version")
    return int(raw) if raw is not None else None


def _drop_schema(tx: Tx) -> None:
    for table in _TABLES:
        tx.execute(f"DROP TABLE IF EXISTS {table}")


def _execute_schema_sql(tx: Tx, sql: str) -> None:
    """Run schema DDL one statement at a time on the existing connection.

    ``conn.executescript`` issues an implicit ``COMMIT`` before its body, which
    would commit a pending drop step inside the rebuild and break atomicity.
    Splitting on ``sqlite3.complete_statement`` avoids that.
    """
    buf: list[str] = []
    for line in sql.splitlines():
        buf.append(line)
        text = "\n".join(buf).strip()
        if text and sqlite3.complete_statement(text):
            tx.execute(text)
            buf = []
    if "\n".join(buf).strip():
        raise sqlite3.OperationalError("incomplete schema SQL")


def _create_schema(tx: Tx, root_path: str) -> None:
    sql = resources.files("ocfl_index").joinpath("schema.sql").read_text()
    _execute_schema_sql(tx, sql)
    now = now_iso()
    for key, value in (
        ("schema_version", str(SCHEMA_VERSION)),
        ("root_path", root_path),
        ("created_at", now),
    ):
        meta_set(tx, key, value)
