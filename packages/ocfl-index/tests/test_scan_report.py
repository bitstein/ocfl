from pathlib import Path

from index_fixtures import FixtureRoot

from ocfl_index import Index
from ocfl_lib import DigestAlgorithm


def test_counters_reflect_actual_changes(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit(
        "alpha", {"a.txt": "1", "b.txt": "2"}, algorithm=DigestAlgorithm.SHA256
    )
    fixture.commit("beta", {"x.txt": "3"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        report = idx.refresh()
        assert report.objects_scanned == 2
        assert report.objects_changed == 2
        assert report.versions_added == 2
        assert report.blobs_added == 3
        assert report.objects_purged == 0

        fixture.commit(
            "alpha",
            {"a.txt": "1-mod"},
            algorithm=DigestAlgorithm.SHA256,
            created="2024-06-01T00:00:00+00:00",
        )
        fixture.remove_object("beta")
        report2 = idx.refresh()
        assert report2.objects_scanned == 1
        assert report2.objects_changed == 1
        assert report2.versions_added == 1
        assert report2.blobs_added == 1
        assert report2.objects_purged == 1


def test_elapsed_seconds_monotonic_positive(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "x"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        report = idx.refresh()
        assert report.elapsed_seconds > 0.0
