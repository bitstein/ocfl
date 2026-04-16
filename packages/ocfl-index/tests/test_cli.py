import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from ocfl_index.cli import app
from ocfl_lib import DigestAlgorithm
from tests.conftest import FixtureRoot

runner = CliRunner()


def _db(tmp_path: Path) -> Path:
    return tmp_path / "i.db"


def test_init_creates_db(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    db_path = _db(tmp_path)
    result = runner.invoke(app, ["init", "--root", fixture.path, "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert db_path.exists()


def test_refresh_reports_counts(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "x"}, algorithm=DigestAlgorithm.SHA256)
    db_path = _db(tmp_path)

    runner.invoke(app, ["init", "--root", fixture.path, "--db", str(db_path)])
    result = runner.invoke(app, ["refresh", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output.strip().splitlines()[-1])
    assert data["objects_scanned"] == 1
    assert data["objects_changed"] == 1


def test_status_shows_row_counts(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "x"}, algorithm=DigestAlgorithm.SHA256)
    db_path = _db(tmp_path)

    runner.invoke(app, ["init", "--root", fixture.path, "--db", str(db_path)])
    runner.invoke(app, ["refresh", "--db", str(db_path)])

    result = runner.invoke(app, ["status", "--db", str(db_path), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert set(data["counts"]) == {
        "objects",
        "versions",
        "content_paths",
        "blobs",
        "ots_attestations",
        "ots_inventory",
    }
    assert data["counts"]["objects"] == 1
    assert data["root_path"] == fixture.path
    assert data["schema_version"] == "1"
    assert data["last_scan_at"] is not None
    assert "last_full_scan_at" in data


def test_show_renders_object_and_head_paths(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit(
        "alpha",
        {"a.txt": "x", "sub/b.txt": "y"},
        algorithm=DigestAlgorithm.SHA256,
    )
    db_path = _db(tmp_path)

    runner.invoke(app, ["init", "--root", fixture.path, "--db", str(db_path)])
    runner.invoke(app, ["refresh", "--db", str(db_path)])

    result = runner.invoke(app, ["show", "alpha", "--db", str(db_path), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert set(data) == {"object", "versions", "head_paths"}
    assert data["object"]["object_id"] == "alpha"
    assert [v["version_num"] for v in data["versions"]] == [1]
    paths = sorted(p["logical_path"] for p in data["head_paths"])
    assert paths == ["a.txt", "sub/b.txt"]


def test_show_missing_object_exit_code_1(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    db_path = _db(tmp_path)
    runner.invoke(app, ["init", "--root", fixture.path, "--db", str(db_path)])
    result = runner.invoke(app, ["show", "nope", "--db", str(db_path)])
    assert result.exit_code == 1


def test_usage_error_exit_code_2(tmp_path: Path) -> None:
    db_path = tmp_path / "missing.db"
    result = runner.invoke(app, ["status", "--db", str(db_path)])
    assert result.exit_code == 2


def test_refresh_error_exit_code_1(tmp_path: Path) -> None:
    fixture = FixtureRoot(tmp_path)
    fixture.commit("alpha", {"a.txt": "x"}, algorithm=DigestAlgorithm.SHA256)
    db_path = _db(tmp_path)
    runner.invoke(app, ["init", "--root", fixture.path, "--db", str(db_path)])

    with patch("ocfl_index.scanner.scan", side_effect=RuntimeError("boom")):
        result = runner.invoke(app, ["refresh", "--db", str(db_path)])
    assert result.exit_code == 1
