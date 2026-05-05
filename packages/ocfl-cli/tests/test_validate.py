"""Tests for ocfl validate command."""

import json
from pathlib import Path

from typer.testing import CliRunner

from ocfl_cli.main import app


class TestValidateRoot:
    def test_valid_root(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "validate"])
        assert result.exit_code == 0
        assert "Valid" in result.stdout

    def test_valid_root_json(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "--json", "validate"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["valid"] is True

    def test_invalid_root(self, runner: CliRunner, tmp_path: Path):
        result = runner.invoke(app, ["--root", str(tmp_path), "validate"])
        assert result.exit_code == 1

    def test_nonexistent_root(self, runner: CliRunner, tmp_path: Path):
        bad = tmp_path / "nope"
        result = runner.invoke(app, ["--root", str(bad), "validate"])
        assert result.exit_code == 1

    def test_no_check_digests(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "validate", "--no-check-digests"]
        )
        assert result.exit_code == 0

    def test_root_json_reports_invalid_contained_object(
        self, runner: CliRunner, storage_root: Path
    ):
        """Root validation must fail when a discovered object is invalid."""
        from ocfl_lib import LocalFS, StorageRoot

        root = StorageRoot(LocalFS(storage_root))
        obj_path = storage_root / root.object_path("obj:test")
        inv_path = obj_path / "inventory.json"
        inv = json.loads(inv_path.read_text())
        inv["digestAlgorithm"] = "md5"
        inv_path.write_text(json.dumps(inv, separators=(",", ":")))

        result = runner.invoke(app, ["--root", str(storage_root), "--json", "validate"])
        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["valid"] is False
        assert any(e["code"] == "E025a" for e in data["errors"])


class TestValidateObject:
    def test_valid_object(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "validate", "obj:test"]
        )
        assert result.exit_code == 0
        assert "Valid" in result.stdout

    def test_valid_object_json(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "--json", "validate", "obj:test"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["valid"] is True
        assert data["errors"] == []

    def test_invalid_object_id(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "validate", "nonexistent"]
        )
        assert result.exit_code == 1

    def test_no_check_digests(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "validate",
                "obj:test",
                "--no-check-digests",
            ],
        )
        assert result.exit_code == 0
