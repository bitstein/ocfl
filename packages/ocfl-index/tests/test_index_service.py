from pathlib import Path

from index_fixtures import FixtureRoot

from ocfl_index import Index, ObjectDetail, StatusReport


def _index(tmp_path: Path, fixture: FixtureRoot) -> Index:
    db_path = tmp_path / "i.db"
    idx = Index.open(db_path, fixture.path)
    idx.refresh(full=True)
    return idx


def test_counts_returns_six_table_counts(
    tmp_path: Path, fixture_root: FixtureRoot
) -> None:
    fixture_root.commit("alpha", {"a.txt": "x"})
    with _index(tmp_path, fixture_root) as idx:
        counts = idx.counts()
    assert set(counts) == {
        "objects",
        "versions",
        "content_paths",
        "blobs",
        "ots_attestations",
        "ots_inventory",
    }
    assert counts["objects"] == 1
    assert counts["versions"] == 1
    assert counts["content_paths"] == 1
    assert counts["blobs"] == 1


def test_status_returns_populated_status_report(
    tmp_path: Path, fixture_root: FixtureRoot
) -> None:
    fixture_root.commit("alpha", {"a.txt": "x"})
    with _index(tmp_path, fixture_root) as idx:
        report = idx.status()
    assert isinstance(report, StatusReport)
    assert report.root_path == fixture_root.path
    assert report.schema_version == "1"
    assert report.last_scan_at is not None
    assert report.last_full_scan_at is not None
    assert report.counts["objects"] == 1


def test_get_object_detail_bundles_object_versions_head_paths(
    tmp_path: Path, fixture_root: FixtureRoot
) -> None:
    fixture_root.commit("alpha", {"a.txt": "x", "sub/b.txt": "y"})
    fixture_root.commit(
        "alpha",
        {"a.txt": "x", "sub/b.txt": "y", "c.txt": "z"},
        created="2024-02-01T00:00:00+00:00",
    )
    with _index(tmp_path, fixture_root) as idx:
        detail = idx.get_object_detail("alpha")
    assert isinstance(detail, ObjectDetail)
    assert detail.object.object_id == "alpha"
    assert detail.object.head_version_num == 2
    assert isinstance(detail.versions, tuple)
    assert [v.version_num for v in detail.versions] == [1, 2]
    assert isinstance(detail.head_paths, tuple)
    assert [p.logical_path for p in detail.head_paths] == [
        "a.txt",
        "c.txt",
        "sub/b.txt",
    ]


def test_get_object_detail_unknown_returns_none(
    tmp_path: Path, fixture_root: FixtureRoot
) -> None:
    with _index(tmp_path, fixture_root) as idx:
        assert idx.get_object_detail("nope") is None
