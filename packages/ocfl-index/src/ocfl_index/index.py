"""Public Index API: open, refresh, lookup."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Self

from ocfl_index import db
from ocfl_lib import LocalFS, StorageRoot

if TYPE_CHECKING:
    import sqlite3
    from types import TracebackType


class IndexRootMismatch(ValueError):
    """Raised when an index is reopened against a different root path."""


class Index:
    """SQLite cache of a single OCFL storage root."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        storage_root: StorageRoot,
        root_path: str,
    ) -> None:
        self._conn = conn
        self._storage_root = storage_root
        self._root_path = root_path

    @classmethod
    def open(cls, db_path: Path | str, root: Path | str) -> Self:
        root_path = str(Path(root))
        conn = db.connect(db_path)
        db.migrate(conn, root_path=root_path)
        stored = db.meta_get(conn, "root_path")
        if stored != root_path:
            conn.close()
            raise IndexRootMismatch(
                f"index was initialized for root {stored!r}, got {root_path!r}"
            )
        storage_root = StorageRoot(LocalFS(Path(root)))
        return cls(conn, storage_root, root_path)

    @property
    def root_path(self) -> str:
        return self._root_path

    @property
    def storage_root(self) -> StorageRoot:
        return self._storage_root

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
