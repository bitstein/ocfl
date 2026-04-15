"""Walk an OCFL root and update the index cache."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ocfl_index.models import ScanReport
from ocfl_lib import DigestAlgorithm, Object, Spec, VNum
from ocfl_lib import fs as ocfl_fs

if TYPE_CHECKING:
    import sqlite3

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


def _scan_object(
    conn: sqlite3.Connection,
    fs: FS,
    relpath: str,
    counters: _Counters,
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
    start = time.perf_counter()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for relpath in storage_root.object_paths():
            _scan_object(conn, fs, relpath, counters)
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    elapsed = time.perf_counter() - start
    return counters.to_report(elapsed_seconds=elapsed)
