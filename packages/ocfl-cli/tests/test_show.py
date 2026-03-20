"""Tests for ocfl show command."""

import json
from pathlib import Path

from typer.testing import CliRunner

from ocfl_cli.main import app


class TestShow:
    def test_show_head(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "show", "obj:test"])
        assert result.exit_code == 0
        assert "v2" in result.stdout
        # Should show diff vs v1
        assert "file2.txt" in result.stdout

    def test_show_v1(self, runner: CliRunner, storage_root: Path):
        """v1 shows all files as added."""
        result = runner.invoke(
            app, ["--root", str(storage_root), "show", "obj:test", "v1"]
        )
        assert result.exit_code == 0
        assert "v1" in result.stdout
        assert "file1.txt" in result.stdout

    def test_show_bare_version(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "show", "obj:test", "1"]
        )
        assert result.exit_code == 0
        assert "v1" in result.stdout

    def test_show_json(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "--json", "show", "obj:test"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["version"] == "v2"
        assert "changes" in data
        assert data["message"] == "v2"

    def test_show_v1_json(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "--json", "show", "obj:test", "v1"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["version"] == "v1"
        # All files should be "added" for v1
        assert all(c["action"] == "added" for c in data["changes"])

    def test_show_nonexistent(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "show", "nonexistent"]
        )
        assert result.exit_code == 1
