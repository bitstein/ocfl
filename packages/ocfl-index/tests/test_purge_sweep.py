from collections.abc import Iterator
from pathlib import Path

import pytest
from index_fixtures import FixtureRoot

from ocfl_index import Index
from ocfl_index.scanner import scan
from ocfl_lib import DigestAlgorithm


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

        def boom() -> Iterator[str]:
            yield from []
            raise RuntimeError("injected")

        monkeypatch.setattr(idx.storage_root, "object_paths", boom)
        with pytest.raises(RuntimeError, match="injected"):
            scan(idx._conn, idx.storage_root, full=False)

        after = sorted(
            r[0] for r in idx._conn.execute("SELECT object_id FROM objects").fetchall()
        )
        assert after == original
