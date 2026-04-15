"""Walk an OCFL root and update the index cache."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ocfl_index import ots
from ocfl_index.mediatype import OCTET_STREAM, sniff
from ocfl_index.models import ScanReport
from ocfl_lib import DigestAlgorithm, Object, Spec, VNum
from ocfl_lib import fs as ocfl_fs

_SNIFF_PRIMARY_BYTES = 8 * 1024
_SNIFF_RETRY_BYTES = 1024 * 1024

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from ocfl_lib import StorageRoot
    from ocfl_lib.fs.protocols import FS
    from ocfl_lib.inventory import StoredInventory

_log = logging.getLogger(__name__)

_SIDECAR_PROBE_ORDER: tuple[DigestAlgorithm, ...] = (
    DigestAlgorithm.SHA256,
    DigestAlgorithm.SHA512,
)


@dataclass
class _Counters:
    objects_scanned: int = 0
    objects_changed: int = 0
    versions_added: int = 0
    blobs_added: int = 0
    objects_purged: int = 0
    walked_relpaths: set[str] = field(default_factory=set)

    def to_report(self, *, elapsed_seconds: float) -> ScanReport:
        return ScanReport(
            objects_scanned=self.objects_scanned,
            objects_changed=self.objects_changed,
            versions_added=self.versions_added,
            blobs_added=self.blobs_added,
            objects_purged=self.objects_purged,
            elapsed_seconds=elapsed_seconds,
        )


def _read_sidecar(fs: FS, relpath: str) -> tuple[DigestAlgorithm, str] | None:
    """Find the inventory sidecar for an object and return (algo, digest).

    Probes sha256 then sha512. Returns None if neither sidecar is present.
    """
    for alg in _SIDECAR_PROBE_ORDER:
        sidecar = f"{relpath}/inventory.json.{alg.ocfl_name}"
        try:
            text = ocfl_fs.read_text(fs, sidecar)
        except FileNotFoundError:
            continue
        digest = text.strip().split()[0]
        return alg, digest
    _log.warning("no inventory sidecar for %s; skipping", relpath)
    return None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _existing_digest(conn: sqlite3.Connection, relpath: str) -> str | None:
    row = conn.execute(
        "SELECT inventory_digest FROM objects WHERE root_relpath = ?",
        (relpath,),
    ).fetchone()
    return row[0] if row else None


def _upsert_object(
    conn: sqlite3.Connection,
    *,
    relpath: str,
    inventory: StoredInventory,
    inventory_digest: str,
) -> None:
    inv = inventory.inventory
    spec = Spec.from_inventory_type(inv.type)
    head = VNum.parse(inv.head)
    head_info = inv.versions[inv.head]
    conn.execute(
        "INSERT INTO objects(object_id, root_relpath, spec_version, "
        "digest_algorithm, head_version, head_version_num, head_created_at, "
        "inventory_digest, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(object_id) DO UPDATE SET "
        "root_relpath = excluded.root_relpath, "
        "spec_version = excluded.spec_version, "
        "digest_algorithm = excluded.digest_algorithm, "
        "head_version = excluded.head_version, "
        "head_version_num = excluded.head_version_num, "
        "head_created_at = excluded.head_created_at, "
        "inventory_digest = excluded.inventory_digest, "
        "indexed_at = excluded.indexed_at",
        (
            inv.id,
            relpath,
            spec.value,
            inv.digest_algorithm,
            inv.head,
            head.number,
            head_info.created,
            inventory_digest,
            _now(),
        ),
    )


def _existing_version_nums(conn: sqlite3.Connection, object_id: str) -> set[int]:
    rows = conn.execute(
        "SELECT version_num FROM versions WHERE object_id = ?",
        (object_id,),
    ).fetchall()
    return {r[0] for r in rows}


def _insert_versions_and_paths(
    conn: sqlite3.Connection,
    *,
    inventory: StoredInventory,
    counters: _Counters,
) -> None:
    inv = inventory.inventory
    object_id = inv.id
    existing = _existing_version_nums(conn, object_id)
    items = sorted(inv.versions.items(), key=lambda kv: VNum.parse(kv[0]).number)
    for raw_key, info in items:
        vnum = VNum.parse(raw_key)
        if vnum.number in existing:
            continue
        user = info.user
        conn.execute(
            "INSERT INTO versions(object_id, version_num, version, created_at, "
            "message, user_name, user_address) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                object_id,
                vnum.number,
                raw_key,
                info.created,
                info.message,
                user.name if user else None,
                user.address if user else None,
            ),
        )
        path_rows = [
            (object_id, vnum.number, logical_path, digest)
            for digest, paths in info.state.items()
            for logical_path in paths
        ]
        if path_rows:
            conn.executemany(
                "INSERT INTO content_paths(object_id, version_num, "
                "logical_path, digest) VALUES (?, ?, ?, ?)",
                path_rows,
            )
        counters.versions_added += 1


def _file_size(fs: FS, path: str) -> int:
    with fs.open_file(path) as f:
        f.seek(0, 2)
        return f.tell()


def _existing_blob_digests(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT digest FROM blobs").fetchall()
    return {r[0] for r in rows}


def _insert_blobs(
    conn: sqlite3.Connection,
    fs: FS,
    *,
    relpath: str,
    inventory: StoredInventory,
    seen: set[str],
    size_cache: dict[str, int],
    counters: _Counters,
) -> None:
    inv = inventory.inventory
    now = _now()
    for digest, content_paths in inv.manifest.items():
        if digest in seen:
            continue
        if digest not in size_cache:
            size_cache[digest] = _file_size(fs, f"{relpath}/{content_paths[0]}")
        conn.execute(
            "INSERT OR IGNORE INTO blobs(digest, size, media_type, first_seen_at) "
            "VALUES (?, ?, NULL, ?)",
            (digest, size_cache[digest], now),
        )
        seen.add(digest)
        counters.blobs_added += 1


def _scan_object(
    conn: sqlite3.Connection,
    fs: FS,
    relpath: str,
    counters: _Counters,
    *,
    blob_seen: set[str],
    size_cache: dict[str, int],
) -> None:
    counters.walked_relpaths.add(relpath)
    counters.objects_scanned += 1
    sidecar = _read_sidecar(fs, relpath)
    if sidecar is None:
        return
    _, incoming_digest = sidecar
    if _existing_digest(conn, relpath) == incoming_digest:
        return
    obj = Object(fs, relpath)
    inventory = obj.inventory
    _upsert_object(
        conn,
        relpath=relpath,
        inventory=inventory,
        inventory_digest=incoming_digest,
    )
    counters.objects_changed += 1
    _insert_versions_and_paths(conn, inventory=inventory, counters=counters)
    _insert_blobs(
        conn,
        fs,
        relpath=relpath,
        inventory=inventory,
        seen=blob_seen,
        size_cache=size_cache,
        counters=counters,
    )
    _ingest_ots_companions(conn, fs, relpath=relpath, inventory=inventory)


def _ingest_ots_companions(
    conn: sqlite3.Connection,
    fs: FS,
    *,
    relpath: str,
    inventory: StoredInventory,
) -> None:
    inv = inventory.inventory
    seen_in_object: set[str] = set()
    for version_info in inv.versions.values():
        for digest, logical_paths in version_info.state.items():
            if not any(p.endswith(".ots") for p in logical_paths):
                continue
            if digest in seen_in_object:
                continue
            seen_in_object.add(digest)
            content_paths = inv.manifest.get(digest)
            if not content_paths:
                continue
            with fs.open_file(f"{relpath}/{content_paths[0]}") as f:
                proof_bytes = f.read()
            ots.upsert_attestation(conn, proof_bytes)


def _content_path_for_digest(
    conn: sqlite3.Connection, fs: FS, digest: str
) -> str | None:
    """Resolve any physical path under root that stores ``digest``."""
    row = conn.execute(
        "SELECT objects.root_relpath FROM objects "
        "JOIN content_paths ON content_paths.object_id = objects.object_id "
        "WHERE content_paths.digest = ? LIMIT 1",
        (digest,),
    ).fetchone()
    if row is None:
        return None
    relpath = row[0]
    obj = Object(fs, relpath)
    cp = obj.content_path(digest)
    if cp is None:
        return None
    return f"{relpath}/{cp}"


def _read_prefix(fs: FS, path: str, size: int) -> bytes:
    with fs.open_file(path) as f:
        return f.read(size)


def _sniff_blob(fs: FS, path: str, sniffer: Callable[[bytes], str] = sniff) -> str:
    buf = _read_prefix(fs, path, _SNIFF_PRIMARY_BYTES)
    result = sniffer(buf)
    if result and result != OCTET_STREAM:
        return result
    bigger = _read_prefix(fs, path, _SNIFF_RETRY_BYTES)
    retry = sniffer(bigger)
    if retry and retry != OCTET_STREAM:
        return retry
    return OCTET_STREAM


def sniff_pass(
    conn: sqlite3.Connection,
    fs: FS,
    *,
    sniffer: Callable[[bytes], str] = sniff,
) -> int:
    """Populate media_type for blobs where it is NULL. Returns row count."""
    rows = conn.execute("SELECT digest FROM blobs WHERE media_type IS NULL").fetchall()
    updated = 0
    for (digest,) in rows:
        path = _content_path_for_digest(conn, fs, digest)
        if path is None:
            continue
        media_type = _sniff_blob(fs, path, sniffer=sniffer)
        conn.execute(
            "UPDATE blobs SET media_type = ? WHERE digest = ?",
            (media_type, digest),
        )
        updated += 1
    return updated


def sweep_objects(conn: sqlite3.Connection, walked: set[str]) -> int:
    """Delete object rows whose root_relpath is not in walked. Returns count."""
    rows = conn.execute("SELECT root_relpath FROM objects").fetchall()
    db_relpaths = {r[0] for r in rows}
    missing = db_relpaths - walked
    if not missing:
        return 0
    conn.executemany(
        "DELETE FROM objects WHERE root_relpath = ?",
        [(r,) for r in missing],
    )
    return len(missing)


def scan(
    conn: sqlite3.Connection,
    storage_root: StorageRoot,
    *,
    full: bool,
) -> ScanReport:
    """Walk the storage root and update the index cache."""
    import time

    fs: FS = storage_root._fsys
    counters = _Counters()
    blob_seen = _existing_blob_digests(conn)
    size_cache: dict[str, int] = {}
    start = time.perf_counter()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for relpath in storage_root.object_paths():
            _scan_object(
                conn,
                fs,
                relpath,
                counters,
                blob_seen=blob_seen,
                size_cache=size_cache,
            )
    except Exception:
        conn.rollback()
        _log.warning("scan failed mid-walk; skipping purge sweep")
        raise
    counters.objects_purged = sweep_objects(conn, counters.walked_relpaths)
    sniff_pass(conn, fs)
    conn.commit()
    elapsed = time.perf_counter() - start
    return counters.to_report(elapsed_seconds=elapsed)
