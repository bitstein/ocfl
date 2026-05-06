from pathlib import Path

import pytest
from index_fixtures import FixtureRoot

from ocfl_index import Index, ObjectRow
from ocfl_lib import DigestAlgorithm


def test_get_object_returns_frozen_row(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "x"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        idx.refresh()
        row = idx.get_object("alpha")
        assert isinstance(row, ObjectRow)
        assert row.object_id == "alpha"
        assert row.digest_algorithm == "sha256"


def test_open_blob_yields_file_handle(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "hello"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        idx.refresh()
        with idx.open_blob("alpha", 1, "a.txt") as fh:
            assert fh.read() == b"hello"


def test_open_blob_unknown_object_raises_key_error(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "hello"}, algorithm=DigestAlgorithm.SHA256)

    with Index.open(tmp_path / "i.db", fixture.path) as idx:
        idx.refresh()
        with pytest.raises(KeyError, match="object 'missing' not in index"):
            idx.open_blob("missing", 1, "a.txt")
