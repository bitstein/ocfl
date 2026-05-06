"""Public Index API: open, refresh, lookup."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self

from ocfl_index import db, scanner
from ocfl_index.models import (
    BlobRow,
    ObjectDetail,
    ObjectRow,
    OtsAttestation,
    PathRow,
    ScanReport,
    StatusReport,
    VersionRow,
)
from ocfl_lib import LocalFS, StorageRoot, VNum

_COUNT_TABLES = (
    "objects",
    "versions",
    "content_paths",
    "blobs",
    "ots_attestations",
    "ots_content",
    "ots_inventory",
)


class IndexRootMismatch(ValueError):
    """Raised when an index is reopened against a different root path."""


def _stored_root_path(conn: sqlite3.Connection) -> str | None:
    meta_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    return db.meta_get(conn, "root_path") if meta_exists else None


def _open_root_path(
    conn: sqlite3.Connection,
    db_path: Path | str,
    root: Path | str | None,
) -> str:
    stored = _stored_root_path(conn)
    if root is None:
        if not stored:
            conn.close()
            raise IndexRootMismatch(f"index db has no recorded root_path: {db_path}")
        root_path = stored
    else:
        root_path = str(Path(root))
        if stored is not None and stored != root_path:
            conn.close()
            raise IndexRootMismatch(
                f"index was initialized for root {stored!r}, got {root_path!r}"
            )

    db.ensure_schema(conn, root_path=root_path)
    # Catch malformed meta or future schema paths that fail to preserve root_path.
    stored_after = db.meta_get(conn, "root_path")
    if stored_after != root_path:
        conn.close()
        raise IndexRootMismatch(
            f"index was initialized for root {stored_after!r}, got {root_path!r}"
        )
    return root_path


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
    def open(cls, db_path: Path | str, root: Path | str | None = None) -> Self:
        conn = db.connect(db_path)
        root_path = _open_root_path(conn, db_path, root)
        storage_root = StorageRoot(LocalFS(Path(root_path)))
        return cls(conn, storage_root, root_path)

    @property
    def root_path(self) -> str:
        return self._root_path

    @property
    def storage_root(self) -> StorageRoot:
        return self._storage_root

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
        return scanner.scan(self._conn, self._storage_root, full=full)

    def counts(self) -> dict[str, int]:
        return {
            table: self._conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in _COUNT_TABLES
        }

    def status(self) -> StatusReport:
        return StatusReport(
            root_path=self._root_path,
            schema_version=db.meta_get(self._conn, "schema_version"),
            last_scan_at=db.meta_get(self._conn, "last_scan_at"),
            last_full_scan_at=db.meta_get(self._conn, "last_full_scan_at"),
            counts=self.counts(),
        )

    def get_object_detail(self, object_id: str) -> ObjectDetail | None:
        obj = self.get_object(object_id)
        if obj is None:
            return None
        version_rows = self._conn.execute(
            "SELECT object_id, version_num, version, created_at, message, "
            "user_name, user_address FROM versions "
            "WHERE object_id = ? ORDER BY version_num",
            (object_id,),
        ).fetchall()
        versions = tuple(VersionRow(*r) for r in version_rows)
        head_paths = tuple(self.list_paths(object_id, obj.head_version_num))
        return ObjectDetail(object=obj, versions=versions, head_paths=head_paths)

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
        if self.get_object(object_id) is None:
            raise KeyError(f"object {object_id!r} not in index")
        obj = self._storage_root.get_object(object_id)
        return obj.open_logical_path(VNum(version_num), logical_path)

    def get_attestation(self, proof_digest: str) -> OtsAttestation | None:
        row = self._conn.execute(
            "SELECT proof_digest, attested_digest, block_height, parsed_at, "
            "parse_error FROM ots_attestations WHERE proof_digest = ?",
            (proof_digest,),
        ).fetchone()
        return OtsAttestation(*row) if row else None

    def content_attestation(
        self, object_id: str, version_num: int, logical_path: str
    ) -> OtsAttestation | None:
        row = self._conn.execute(
            "SELECT proof_digest FROM ots_content WHERE object_id = ? AND "
            "version_num = ? AND logical_path = ?",
            (object_id, version_num, f"{logical_path}.ots"),
        ).fetchone()
        if row is None:
            return None
        return self.get_attestation(row[0])

    def get_inventory_attestation(
        self, object_id: str, version_num: int
    ) -> OtsAttestation | None:
        row = self._conn.execute(
            "SELECT proof_digest FROM ots_inventory "
            "WHERE object_id = ? AND version_num = ?",
            (object_id, version_num),
        ).fetchone()
        if row is None:
            return None
        return self.get_attestation(row[0])

    def first_version_attestation(self, object_id: str) -> OtsAttestation | None:
        return self.get_inventory_attestation(object_id, 1)
