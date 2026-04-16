from pathlib import Path

from ocfl_index import Index
from ocfl_index.scanner import scan
from ocfl_lib import DigestAlgorithm
from tests.conftest import FixtureRoot


def test_unchanged_root_writes_zero_rows(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "hello"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        scan(idx._conn, idx.storage_root, full=False)
        report = scan(idx._conn, idx.storage_root, full=False)
        assert report.objects_scanned == 1
        assert report.objects_changed == 0


def test_new_version_adds_exactly_one_versions_row(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "v1"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        scan(idx._conn, idx.storage_root, full=False)
        before = idx._conn.execute(
            "SELECT count(*) FROM versions WHERE object_id = 'alpha'"
        ).fetchone()[0]
        assert before == 1

        fixture.commit(
            "alpha",
            {"a.txt": "v2"},
            algorithm=DigestAlgorithm.SHA256,
            created="2024-06-01T00:00:00+00:00",
        )
        report = scan(idx._conn, idx.storage_root, full=False)
        assert report.versions_added == 1

        after = idx._conn.execute(
            "SELECT count(*) FROM versions WHERE object_id = 'alpha'"
        ).fetchone()[0]
        assert after == 2


def test_existing_versions_never_updated(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit(
        "alpha",
        {"a.txt": "v1"},
        algorithm=DigestAlgorithm.SHA256,
        message="first",
    )

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        scan(idx._conn, idx.storage_root, full=False)
        before = idx._conn.execute(
            "SELECT message FROM versions WHERE object_id = 'alpha' AND version_num = 1"
        ).fetchone()[0]

        fixture.commit(
            "alpha",
            {"a.txt": "v2"},
            algorithm=DigestAlgorithm.SHA256,
            message="second",
            created="2024-06-01T00:00:00+00:00",
        )
        scan(idx._conn, idx.storage_root, full=False)
        after = idx._conn.execute(
            "SELECT message FROM versions WHERE object_id = 'alpha' AND version_num = 1"
        ).fetchone()[0]
        assert before == after


def test_head_digest_changes_on_new_version(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "v1"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        scan(idx._conn, idx.storage_root, full=False)
        first_digest = idx._conn.execute(
            "SELECT inventory_digest FROM objects WHERE object_id = 'alpha'"
        ).fetchone()[0]

        fixture.commit(
            "alpha",
            {"a.txt": "v2"},
            algorithm=DigestAlgorithm.SHA256,
            created="2024-06-01T00:00:00+00:00",
        )

        report = scan(idx._conn, idx.storage_root, full=False)
        assert report.objects_changed == 1

        second_digest = idx._conn.execute(
            "SELECT inventory_digest FROM objects WHERE object_id = 'alpha'"
        ).fetchone()[0]
        assert first_digest != second_digest

        head_num = idx._conn.execute(
            "SELECT head_version_num FROM objects WHERE object_id = 'alpha'"
        ).fetchone()[0]
        assert head_num == 2
