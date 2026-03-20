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
