from pathlib import Path

from ocfl_index import Index, db
from ocfl_lib import DigestAlgorithm
from tests.conftest import FixtureRoot


def test_full_rebuild_matches_prior_heads(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "1"}, algorithm=DigestAlgorithm.SHA256)
    fixture.commit("beta", {"b.txt": "2"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        idx.refresh()
        before = dict(idx.iter_heads())
        idx.refresh(full=True)
        after = dict(idx.iter_heads())
        assert before == after


def test_full_rebuild_updates_last_full_scan_at(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "1"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        idx.refresh()
        assert db.meta_get(idx.conn, "last_full_scan_at") is None
        idx.refresh(full=True)
        stamp = db.meta_get(idx.conn, "last_full_scan_at")
        assert stamp is not None


def test_rebuild_clears_ots_tables(tmp_path: Path) -> None:
    from tests.test_ots import _commit_with_inventory_proof

    fixture = FixtureRoot(tmp_path)
    _commit_with_inventory_proof(
        fixture, "alpha", {"a.txt": b"x"}, version_num=1, height=910_000
    )

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        idx.refresh()
        before = idx.conn.execute("SELECT count(*) FROM ots_attestations").fetchone()[0]
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
        after = idx.conn.execute("SELECT count(*) FROM ots_attestations").fetchone()[0]
        assert after == 0
