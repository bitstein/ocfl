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
        report = scan(idx.conn, idx.storage_root, full=False)
        assert report.objects_scanned == 2
        assert report.objects_changed == 2

        rows = idx.conn.execute(
            "SELECT object_id, digest_algorithm FROM objects ORDER BY object_id"
        ).fetchall()
        assert rows == [("alpha", "sha256"), ("beta", "sha512")]
