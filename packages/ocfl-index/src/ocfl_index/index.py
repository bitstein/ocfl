"""Public Index API: open, refresh, lookup."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Self

from ocfl_index import db
from ocfl_index.models import BlobRow, ObjectRow, PathRow, ScanReport, VersionRow
from ocfl_lib import LocalFS, Object, StorageRoot, VNum

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
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

    def refresh(self, *, full: bool = False) -> ScanReport:
        from ocfl_index import scanner

        return scanner.scan(self._conn, self._storage_root, full=full)

    def iter_heads(self) -> Iterator[tuple[str, str]]:
        yield from self._conn.execute("SELECT object_id, inventory_digest FROM objects")

    def get_object(self, object_id: str) -> ObjectRow | None:
        row = self._conn.execute(
            "SELECT object_id, root_relpath, spec_version, digest_algorithm, "
            "head_version, head_version_num, head_created_at, "
            "inventory_digest, indexed_at FROM objects WHERE object_id = ?",
            (object_id,),
        ).fetchone()
        return ObjectRow(*row) if row else None

    def get_version(self, object_id: str, version_num: int) -> VersionRow | None:
        row = self._conn.execute(
            "SELECT object_id, version_num, version, created_at, message, "
            "user_name, user_address FROM versions "
            "WHERE object_id = ? AND version_num = ?",
            (object_id, version_num),
        ).fetchone()
        return VersionRow(*row) if row else None

    def list_paths(self, object_id: str, version_num: int) -> Iterator[PathRow]:
        rows = self._conn.execute(
            "SELECT object_id, version_num, logical_path, digest "
            "FROM content_paths WHERE object_id = ? AND version_num = ? "
            "ORDER BY logical_path",
            (object_id, version_num),
        )
        for r in rows:
            yield PathRow(*r)

    def get_blob(self, digest: str) -> BlobRow | None:
        row = self._conn.execute(
            "SELECT digest, size, media_type, first_seen_at FROM blobs "
            "WHERE digest = ?",
            (digest,),
        ).fetchone()
        return BlobRow(*row) if row else None

    def open_blob(
        self, object_id: str, version_num: int, logical_path: str
    ) -> BinaryIO:
        obj_row = self.get_object(object_id)
        if obj_row is None:
            raise KeyError(f"object {object_id!r} not in index")
        fs = self._storage_root._fsys
        obj = Object(fs, obj_row.root_relpath)
        return obj.open_logical_path(VNum(version_num), logical_path)
