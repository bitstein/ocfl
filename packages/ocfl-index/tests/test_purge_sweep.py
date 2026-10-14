import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from index_fixtures import FixtureRoot, build_proof

from ocfl_index import Index, db, scanner
from ocfl_index.scanner import scan
from ocfl_lib import DigestAlgorithm
from ocfl_lib.fs import FileRef

_SCANNER_LOGGER = "ocfl_index.scanner"


def test_removed_object_row_deleted(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "x"}, algorithm=DigestAlgorithm.SHA256)
    fixture.commit("beta", {"b.txt": "y"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        scan(idx._conn, idx.storage_root, full=False)
        fixture.remove_object("beta")
        report = scan(idx._conn, idx.storage_root, full=False)
        assert report.objects_purged == 1

        rows = idx._conn.execute(
            "SELECT object_id FROM objects ORDER BY object_id"
        ).fetchall()
        assert rows == [("alpha",)]


def test_cascades_to_versions_and_paths(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "x"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        scan(idx._conn, idx.storage_root, full=False)
        fixture.remove_object("alpha")
        scan(idx._conn, idx.storage_root, full=False)

        v = idx._conn.execute(
            "SELECT count(*) FROM versions WHERE object_id = 'alpha'"
        ).fetchone()[0]
        cp = idx._conn.execute(
            "SELECT count(*) FROM content_paths WHERE object_id = 'alpha'"
        ).fetchone()[0]
        assert v == 0
        assert cp == 0


def test_purge_cascades_to_content_ots_links(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    proof = build_proof(b"x", height=911_000)
    fixture.commit(
        "alpha",
        {"a.txt": b"x", "a.txt.ots": proof},
        algorithm=DigestAlgorithm.SHA512,
    )

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        scan(idx._conn, idx.storage_root, full=False)
        before = idx._conn.execute("SELECT count(*) FROM ots_content").fetchone()[0]
        assert before == 1

        fixture.remove_object("alpha")
        scan(idx._conn, idx.storage_root, full=False)
        after = idx._conn.execute("SELECT count(*) FROM ots_content").fetchone()[0]
        assert after == 0


def test_blobs_retained_after_purge(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "shared"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        scan(idx._conn, idx.storage_root, full=False)
        before = idx._conn.execute("SELECT count(*) FROM blobs").fetchone()[0]
        assert before == 1

        fixture.remove_object("alpha")
        scan(idx._conn, idx.storage_root, full=False)
        after = idx._conn.execute("SELECT count(*) FROM blobs").fetchone()[0]
        assert after == 1


def test_partial_walk_failure_skips_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "x"}, algorithm=DigestAlgorithm.SHA256)
    fixture.commit("beta", {"b.txt": "y"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        scan(idx._conn, idx.storage_root, full=False)
        original = sorted(
            r[0] for r in idx._conn.execute("SELECT object_id FROM objects").fetchall()
        )

        fixture.remove_object("beta")

        def boom(*, strict: bool = False) -> Iterator[str]:
            yield from []
            raise RuntimeError("injected")

        monkeypatch.setattr(idx.storage_root, "object_paths", boom)
        with pytest.raises(RuntimeError, match="injected"):
            scan(idx._conn, idx.storage_root, full=False)

        after = sorted(
            r[0] for r in idx._conn.execute("SELECT object_id FROM objects").fetchall()
        )
        assert after == original


def test_partial_walk_failure_logs_warning_before_first_yield(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "x"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        scan(idx._conn, idx.storage_root, full=False)

        def boom(*, strict: bool = False) -> Iterator[str]:
            yield from []
            raise RuntimeError("injected-pre-yield")

        monkeypatch.setattr(idx.storage_root, "object_paths", boom)
        with (
            caplog.at_level(logging.WARNING, logger=_SCANNER_LOGGER),
            pytest.raises(RuntimeError, match="injected-pre-yield"),
        ):
            scan(idx._conn, idx.storage_root, full=False)

    records = [
        r
        for r in caplog.records
        if r.name == _SCANNER_LOGGER and "scan failed" in r.message
    ]
    assert len(records) == 1, [r.message for r in records]
    rec = records[0]
    assert "rolled back refresh transaction" in rec.message
    assert rec.__dict__["relpath"] is None


def test_partial_walk_failure_logs_warning_with_current_relpath(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "x"}, algorithm=DigestAlgorithm.SHA256)
    fixture.commit("beta", {"b.txt": "y"}, algorithm=DigestAlgorithm.SHA256)

    real_scan_object = scanner._scan_object
    failing_relpath = "beta"

    def fail_on_beta(conn, fs, relpath, counters, **kwargs):
        if relpath == failing_relpath:
            raise RuntimeError("injected-mid-walk")
        return real_scan_object(conn, fs, relpath, counters, **kwargs)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        monkeypatch.setattr(scanner, "_scan_object", fail_on_beta)
        with (
            caplog.at_level(logging.WARNING, logger=_SCANNER_LOGGER),
            pytest.raises(RuntimeError, match="injected-mid-walk"),
        ):
            scan(idx._conn, idx.storage_root, full=False)

    records = [
        r
        for r in caplog.records
        if r.name == _SCANNER_LOGGER and "scan failed" in r.message
    ]
    assert len(records) == 1, [r.message for r in records]
    rec = records[0]
    assert "rolled back refresh transaction" in rec.message
    assert rec.__dict__["relpath"] == failing_relpath
    assert f"scan failed during object walk at {failing_relpath}" in rec.message


def test_oserror_partial_walk_skips_sweep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "x"}, algorithm=DigestAlgorithm.SHA256)
    fixture.commit("beta", {"b.txt": "y"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        scan(idx._conn, idx.storage_root, full=False)
        original = sorted(
            r[0] for r in idx._conn.execute("SELECT object_id FROM objects").fetchall()
        )
        assert original == ["alpha", "beta"]

        def torn_walk(_name: str) -> Iterator[FileRef]:
            yield FileRef(path="alpha/0=ocfl_object_1.1", size=0)
            raise OSError("injected")

        monkeypatch.setattr(idx.storage_root._fsys, "walk_files", torn_walk)
        with (
            caplog.at_level(logging.WARNING, logger=_SCANNER_LOGGER),
            pytest.raises(OSError, match="injected"),
        ):
            scan(idx._conn, idx.storage_root, full=False)

        after = sorted(
            r[0] for r in idx._conn.execute("SELECT object_id FROM objects").fetchall()
        )
        assert after == original

    records = [
        r
        for r in caplog.records
        if r.name == _SCANNER_LOGGER and "scan failed" in r.message
    ]
    assert len(records) == 1, [r.message for r in records]
    rec = records[0]
    assert "rolled back refresh transaction" in rec.message


def test_sweep_failure_rolls_back_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "x"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        scan(idx._conn, idx.storage_root, full=False)
        fixture.commit("beta", {"b.txt": "y"}, algorithm=DigestAlgorithm.SHA256)

        def boom(_conn, _walked) -> int:
            raise RuntimeError("injected-sweep")

        monkeypatch.setattr(scanner, "sweep_objects", boom)
        with pytest.raises(RuntimeError, match="injected-sweep"):
            scan(idx._conn, idx.storage_root, full=False)

        rows = idx._conn.execute(
            "SELECT object_id FROM objects ORDER BY object_id"
        ).fetchall()
        assert rows == [("alpha",)]

        with db.immediate_transaction(idx._conn):
            pass


def test_sniff_failure_rolls_back_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "x"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        scan(idx._conn, idx.storage_root, full=False)
        fixture.commit("beta", {"b.txt": "y"}, algorithm=DigestAlgorithm.SHA256)

        def boom(_conn, _fs) -> int:
            raise RuntimeError("injected-sniff")

        monkeypatch.setattr(scanner, "sniff_pass", boom)
        with pytest.raises(RuntimeError, match="injected-sniff"):
            scan(idx._conn, idx.storage_root, full=False)

        rows = idx._conn.execute(
            "SELECT object_id FROM objects ORDER BY object_id"
        ).fetchall()
        assert rows == [("alpha",)]

        with db.immediate_transaction(idx._conn):
            pass


def test_commit_failure_releases_writer_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deferred-FK violations surface at COMMIT; scanner must release the lock."""
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "x"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        scan(idx._conn, idx.storage_root, full=False)

        def inject_deferred_violation(conn: sqlite3.Connection, _fs: object) -> int:
            # PRAGMA resets at COMMIT/ROLLBACK, so it's set per refresh tx.
            conn.execute("PRAGMA defer_foreign_keys = ON")
            conn.execute(
                "INSERT INTO content_paths(object_id, version_num, "
                "logical_path, digest) VALUES "
                "('does-not-exist', 1, 'p.txt', 'd')"
            )
            return 0

        monkeypatch.setattr(scanner, "sniff_pass", inject_deferred_violation)
        with pytest.raises(sqlite3.IntegrityError):
            scan(idx._conn, idx.storage_root, full=False)

        assert idx._conn.in_transaction is False
        with db.immediate_transaction(idx._conn):
            pass
