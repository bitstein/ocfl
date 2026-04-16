from pathlib import Path

from ocfl_index import Index
from ocfl_index.scanner import scan
from ocfl_lib import DigestAlgorithm
from tests.conftest import FixtureRoot


def test_full_scan_populates_objects(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "hello"}, algorithm=DigestAlgorithm.SHA256)
    fixture.commit("beta", {"b.txt": "world"}, algorithm=DigestAlgorithm.SHA512)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        report = scan(idx._conn, idx.storage_root, full=False)
        assert report.objects_scanned == 2
        assert report.objects_changed == 2

        rows = idx._conn.execute(
            "SELECT object_id, digest_algorithm FROM objects ORDER BY object_id"
        ).fetchall()
        assert rows == [("alpha", "sha256"), ("beta", "sha512")]


def test_versions_rows_match_inventory(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "v1"}, algorithm=DigestAlgorithm.SHA256)
    fixture.commit(
        "alpha",
        {"a.txt": "v2"},
        algorithm=DigestAlgorithm.SHA256,
        created="2024-06-01T00:00:00+00:00",
    )

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        scan(idx._conn, idx.storage_root, full=False)
        rows = idx._conn.execute(
            "SELECT version_num, version FROM versions WHERE object_id = 'alpha' "
            "ORDER BY version_num"
        ).fetchall()
        assert rows == [(1, "v1"), (2, "v2")]


def test_blobs_row_per_unique_digest(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit(
        "alpha",
        {"a.txt": "same", "b.txt": "same"},
        algorithm=DigestAlgorithm.SHA256,
    )

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        scan(idx._conn, idx.storage_root, full=False)
        rows = idx._conn.execute("SELECT count(*) FROM blobs").fetchone()
        assert rows[0] == 1


def test_blob_dedup_across_objects(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"x.txt": "shared"}, algorithm=DigestAlgorithm.SHA256)
    fixture.commit("beta", {"y.txt": "shared"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        scan(idx._conn, idx.storage_root, full=False)
        rows = idx._conn.execute("SELECT count(*) FROM blobs").fetchone()
        assert rows[0] == 1


def test_blob_size_matches_file(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    payload = b"x" * 137
    fixture.commit("alpha", {"a.bin": payload}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        scan(idx._conn, idx.storage_root, full=False)
        size = idx._conn.execute("SELECT size FROM blobs").fetchone()[0]
        assert size == len(payload)


def test_content_paths_rows_match_state(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit(
        "alpha",
        {"a.txt": "alpha-v1", "sub/b.txt": "beta-v1"},
        algorithm=DigestAlgorithm.SHA256,
    )

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        scan(idx._conn, idx.storage_root, full=False)
        paths = idx._conn.execute(
            "SELECT logical_path FROM content_paths "
            "WHERE object_id = 'alpha' AND version_num = 1 "
            "ORDER BY logical_path"
        ).fetchall()
        assert paths == [("a.txt",), ("sub/b.txt",)]
