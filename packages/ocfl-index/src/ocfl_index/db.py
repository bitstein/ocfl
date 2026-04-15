"""SQLite connection and schema management."""

import sqlite3
from datetime import UTC, datetime
from importlib import resources
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

SCHEMA_VERSION = 1

_TABLES = (
    "ots_inventory",
    "ots_attestations",
    "blobs",
    "content_paths",
    "versions",
    "objects",
    "meta",
)

_CACHE_TABLES = (
    "ots_inventory",
    "ots_attestations",
    "blobs",
    "content_paths",
    "versions",
    "objects",
)


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a SQLite connection with WAL + foreign keys enforced."""
    conn = sqlite3.connect(path, timeout=0)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 0")
    return conn


def migrate(conn: sqlite3.Connection, root_path: str) -> None:
    """Create schema if missing; drop+recreate on schema_version mismatch."""
    stored = _stored_schema_version(conn)
    if stored is None:
        _create_schema(conn, root_path)
    elif stored != SCHEMA_VERSION:
        _drop_schema(conn)
        _create_schema(conn, root_path)


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def reset_cache(conn: sqlite3.Connection) -> None:
    """Truncate all index tables (everything except meta)."""
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


def _drop_schema(conn: sqlite3.Connection) -> None:
    with conn:
        for table in _TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")


def _create_schema(conn: sqlite3.Connection, root_path: str) -> None:
    sql = resources.files("ocfl_index").joinpath("schema.sql").read_text()
    now = datetime.now(UTC).isoformat()
    with conn:
        conn.executescript(sql)
        for key, value in (
            ("schema_version", str(SCHEMA_VERSION)),
            ("root_path", root_path),
            ("created_at", now),
        ):
            meta_set(conn, key, value)
