"""Walk an OCFL root and update the index cache."""

import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from ocfl_index import db, ots
from ocfl_index.constants import OTS_EXTENSION_DIR
from ocfl_index.mediatype import OCTET_STREAM, sniff
from ocfl_index.models import ScanReport
from ocfl_lib import DigestAlgorithm, Object, Spec, StorageRoot, VNum
from ocfl_lib import fs as ocfl_fs
from ocfl_lib.fs.protocols import FS
from ocfl_lib.inventory import StoredInventory, VersionInfo

_SNIFF_PRIMARY_BYTES = 8 * 1024
_SNIFF_RETRY_BYTES = 1024 * 1024

_log = logging.getLogger(__name__)


class RefreshInProgress(RuntimeError):
    """Another refresh is already running against this index."""


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
            db.now_iso(),
        ),
    )


def _existing_version_nums(conn: sqlite3.Connection, object_id: str) -> set[int]:
    rows = conn.execute(
        "SELECT version_num FROM versions WHERE object_id = ?",
        (object_id,),
    ).fetchall()
    return {r[0] for r in rows}


def _insert_version(
    conn: sqlite3.Connection,
    object_id: str,
    vnum: VNum,
    raw_key: str,
    info: VersionInfo,
) -> None:
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


def _insert_paths(
    conn: sqlite3.Connection,
    object_id: str,
    vnum: VNum,
    state: dict[str, list[str]],
) -> None:
    path_rows = [
        (object_id, vnum.number, logical_path, digest)
        for digest, paths in state.items()
        for logical_path in paths
    ]
    if path_rows:
        conn.executemany(
            "INSERT INTO content_paths(object_id, version_num, "
            "logical_path, digest) VALUES (?, ?, ?, ?)",
            path_rows,
        )


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
    now = db.now_iso()
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
    walked: set[str],
    blob_seen: set[str],
    size_cache: dict[str, int],
) -> None:
    walked.add(relpath)
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
    inv = inventory.inventory
    object_id = inv.id
    existing = _existing_version_nums(conn, object_id)
    items = sorted(inv.versions.items(), key=lambda kv: VNum.parse(kv[0]).number)
    added_versions: list[tuple[int, str]] = []
    for raw_key, info in items:
        vnum = VNum.parse(raw_key)
        if vnum.number in existing:
            continue
        _insert_version(conn, object_id, vnum, raw_key, info)
        _insert_paths(conn, object_id, vnum, info.state)
        counters.versions_added += 1
        added_versions.append((vnum.number, raw_key))
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
    _ingest_ots_inventory_proofs(
        conn,
        fs,
        relpath=relpath,
        object_id=inventory.inventory.id,
        added_versions=added_versions,
    )


def _ingest_ots_companions(
    conn: sqlite3.Connection,
    fs: FS,
    *,
    relpath: str,
    inventory: StoredInventory,
) -> None:
    inv = inventory.inventory
    proof_digest_by_content_digest: dict[str, str] = {}
    for raw_key, version_info in inv.versions.items():
        vnum = VNum.parse(raw_key)
        for digest, logical_paths in version_info.state.items():
            ots_paths = [p for p in logical_paths if p.endswith(".ots")]
            if not ots_paths:
                continue
            pdigest = proof_digest_by_content_digest.get(digest)
            if pdigest is None:
                content_paths = inv.manifest.get(digest)
                if not content_paths:
                    continue
                with fs.open_file(f"{relpath}/{content_paths[0]}") as f:
                    proof_bytes = f.read()
                result = ots.upsert_attestation(conn, proof_bytes)
                pdigest = result.proof_digest
                proof_digest_by_content_digest[digest] = pdigest
                if result.was_inserted and result.parse_error is not None:
                    first_path = ots_paths[0]
                    _log.warning(
                        "ots proof parse failed for %s: proof_digest=%s error=%s",
                        first_path,
                        pdigest,
                        result.parse_error,
                        extra={
                            "object_id": inv.id,
                            "version_num": vnum.number,
                            "logical_path": first_path,
                            "proof_digest": pdigest,
                        },
                    )
            conn.executemany(
                "INSERT OR REPLACE INTO ots_content(object_id, version_num, "
                "logical_path, proof_digest) VALUES (?, ?, ?, ?)",
                [(inv.id, vnum.number, path, pdigest) for path in ots_paths],
            )


def _ingest_ots_inventory_proofs(
    conn: sqlite3.Connection,
    fs: FS,
    *,
    relpath: str,
    object_id: str,
    added_versions: list[tuple[int, str]],
) -> None:
    base = f"{relpath}/extensions/{OTS_EXTENSION_DIR}"
    for vnum, vkey in added_versions:
        proof_path = f"{base}/inventory.json.{vkey}.ots"
        try:
            with fs.open_file(proof_path) as f:
                proof_bytes = f.read()
        except FileNotFoundError:
            continue
        result = ots.upsert_attestation(conn, proof_bytes)
        pdigest = result.proof_digest
        if result.was_inserted and result.parse_error is not None:
            _log.warning(
                "ots inventory proof parse failed for proof_path=%s: "
                "proof_digest=%s error=%s",
                proof_path,
                pdigest,
                result.parse_error,
                extra={
                    "object_id": object_id,
                    "version_num": vnum,
                    "proof_path": proof_path,
                    "proof_digest": pdigest,
                },
            )
        conn.execute(
            "INSERT OR REPLACE INTO ots_inventory(object_id, version_num, "
            "proof_digest) VALUES (?, ?, ?)",
            (object_id, vnum, pdigest),
        )


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
    """Walk the storage root and update the index cache.

    Single ``BEGIN IMMEDIATE`` tx by design: walk, sweep, sniff, and
    ``meta.last_scan_at`` advance together. Readers see one consistent snapshot;
    a crash rolls back the whole refresh.

    Tradeoff: lock-hold scales with ``sniff_pass`` (per-blob file I/O), so a
    very large root can stall other writers for minutes. Acceptable while
    refresh is the only writer.

    ``sniff_pass`` is idempotent and resumable via ``WHERE media_type IS NULL``;
    splitting it into batched short txs is a clean refactor the day a second
    writer exists.
    """
    fs: FS = storage_root.fs
    counters = _Counters()
    walked: set[str] = set()
    size_cache: dict[str, int] = {}
    start = time.perf_counter()
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as err:
        if db.is_lock_error(err):
            raise RefreshInProgress(
                "another refresh is holding the write lock"
            ) from err
        raise
    current_relpath: str | None = None
    current_phase = "object walk"
    try:
        if full:
            current_phase = "cache reset"
            db.reset_cache(conn)
        current_phase = "blob digest load"
        blob_seen = _existing_blob_digests(conn)
        current_phase = "object walk"
        for relpath in storage_root.object_paths(strict=True):
            current_relpath = relpath
            _scan_object(
                conn,
                fs,
                relpath,
                counters,
                walked=walked,
                blob_seen=blob_seen,
                size_cache=size_cache,
            )
        current_relpath = None
        current_phase = "purge sweep"
        counters.objects_purged = sweep_objects(conn, walked)
        current_phase = "media type sniff"
        sniff_pass(conn, fs)
        current_phase = "metadata update"
        now = db.now_iso()
        db.meta_set(conn, "last_scan_at", now)
        if full:
            db.meta_set(conn, "last_full_scan_at", now)
        conn.commit()
    except Exception as exc:
        failure_at = (
            f"{current_phase} at {current_relpath}"
            if current_relpath is not None
            else current_phase
        )
        _log.warning(
            "scan failed during %s: %s; rolled back refresh transaction",
            failure_at,
            exc,
            extra={"phase": current_phase, "relpath": current_relpath},
            exc_info=True,
        )
        conn.rollback()
        raise
    elapsed = time.perf_counter() - start
    return ScanReport(**vars(counters), elapsed_seconds=elapsed)
