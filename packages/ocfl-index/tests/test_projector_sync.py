from pathlib import Path

from ocfl_index import Index
from ocfl_lib import DigestAlgorithm
from tests.conftest import FixtureRoot


def _heads(idx: Index) -> dict[str, str]:
    return dict(idx.iter_heads())


def test_iter_heads_yields_id_digest_pairs(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a": "1"}, algorithm=DigestAlgorithm.SHA256)
    fixture.commit("beta", {"b": "2"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        idx.refresh()
        heads = _heads(idx)
        assert set(heads) == {"alpha", "beta"}
        for digest in heads.values():
            assert len(digest) == 64


def test_new_object_detected(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a": "1"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        idx.refresh()
        bookmark = _heads(idx)

        fixture.commit("beta", {"b": "1"}, algorithm=DigestAlgorithm.SHA256)
        idx.refresh()
        new = {oid for oid in _heads(idx) if oid not in bookmark}
        assert new == {"beta"}


def test_new_version_detected_via_digest_change(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a": "v1"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        idx.refresh()
        bookmark = _heads(idx)

        fixture.commit(
            "alpha",
            {"a": "v2"},
            algorithm=DigestAlgorithm.SHA256,
            created="2024-06-01T00:00:00+00:00",
        )
        idx.refresh()
        current = _heads(idx)
        assert current["alpha"] != bookmark["alpha"]


def test_purge_detected_as_missing_from_iter_heads(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a": "1"}, algorithm=DigestAlgorithm.SHA256)
    fixture.commit("beta", {"b": "1"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        idx.refresh()
        bookmark = _heads(idx)

        fixture.remove_object("beta")
        idx.refresh()
        purged = set(bookmark) - set(_heads(idx))
        assert purged == {"beta"}
