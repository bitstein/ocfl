from pathlib import Path

from ocfl_index import Index
from ocfl_index.scanner import scan
from ocfl_lib import DigestAlgorithm
from tests.conftest import FixtureRoot


def test_unchanged_root_writes_zero_rows(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "hello"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        scan(idx.conn, idx.storage_root, full=False)
        report = scan(idx.conn, idx.storage_root, full=False)
        assert report.objects_scanned == 1
        assert report.objects_changed == 0


def test_head_digest_changes_on_new_version(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "v1"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        scan(idx.conn, idx.storage_root, full=False)
        first_digest = idx.conn.execute(
            "SELECT inventory_digest FROM objects WHERE object_id = 'alpha'"
        ).fetchone()[0]

        fixture.commit(
            "alpha",
            {"a.txt": "v2"},
            algorithm=DigestAlgorithm.SHA256,
            created="2024-06-01T00:00:00+00:00",
        )

        report = scan(idx.conn, idx.storage_root, full=False)
        assert report.objects_changed == 1

        second_digest = idx.conn.execute(
            "SELECT inventory_digest FROM objects WHERE object_id = 'alpha'"
        ).fetchone()[0]
        assert first_digest != second_digest

        head_num = idx.conn.execute(
            "SELECT head_version_num FROM objects WHERE object_id = 'alpha'"
        ).fetchone()[0]
        assert head_num == 2
