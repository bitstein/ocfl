from pathlib import Path

from index_fixtures import FixtureRoot

from ocfl_index import Index, db
from ocfl_lib import DigestAlgorithm


def _stable_rows(idx: Index) -> dict[str, list[tuple]]:
    return {
        "objects": idx._conn.execute(
            "SELECT object_id, inventory_digest FROM objects ORDER BY object_id"
        ).fetchall(),
        "versions": idx._conn.execute(
            "SELECT object_id, version_num, version, created_at, message, "
            "user_name, user_address FROM versions "
            "ORDER BY object_id, version_num"
        ).fetchall(),
        "content_paths": idx._conn.execute(
            "SELECT object_id, version_num, logical_path, digest FROM content_paths "
            "ORDER BY object_id, version_num, logical_path"
        ).fetchall(),
        "blobs": idx._conn.execute(
            "SELECT digest, size, media_type FROM blobs ORDER BY digest"
        ).fetchall(),
    }


def test_full_rebuild_matches_prior_index_rows(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "1"}, algorithm=DigestAlgorithm.SHA256)
    fixture.commit("beta", {"b.txt": "2"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        idx.refresh()
        before = _stable_rows(idx)
        idx.refresh(full=True)
        after = _stable_rows(idx)
        assert before == after


def test_full_rebuild_updates_last_full_scan_at(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "1"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        idx.refresh()
        assert db.meta_get(idx._conn, "last_full_scan_at") is None
        idx.refresh(full=True)
        stamp = db.meta_get(idx._conn, "last_full_scan_at")
        assert stamp is not None


def test_rebuild_clears_ots_tables(tmp_path: Path) -> None:
    from index_fixtures import commit_with_inventory_proof

    fixture = FixtureRoot(tmp_path)
    commit_with_inventory_proof(
        fixture, "alpha", {"a.txt": b"x"}, version_num=1, height=910_000
    )

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        idx.refresh()
        before = idx._conn.execute("SELECT count(*) FROM ots_attestations").fetchone()[
            0
        ]
        assert before > 0

        # Remove the proof file and rebuild
        relpath = fixture.root.object_path("alpha")
        proof_path = (
            fixture.root_dir
            / relpath
            / "extensions"
            / "NNNN-ots-timestamps"
            / "inventory.json.v1.ots"
        )
        proof_path.unlink()

        idx.refresh(full=True)
        after = idx._conn.execute("SELECT count(*) FROM ots_attestations").fetchone()[0]
        assert after == 0
