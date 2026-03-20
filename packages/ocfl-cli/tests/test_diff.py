"""Tests for ocfl diff command."""

import json
from pathlib import Path

from typer.testing import CliRunner

from ocfl_cli.main import app


class TestDiff:
    def test_diff(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "diff", "obj:test", "v1", "v2"]
        )
        assert result.exit_code == 0
        assert "file2.txt" in result.stdout
        assert "A  file2.txt" in result.stdout

    def test_diff_bare_versions(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "diff", "obj:test", "1", "2"]
        )
        assert result.exit_code == 0
        assert "file2.txt" in result.stdout

    def test_diff_json(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "--json", "diff", "obj:test", "v1", "v2"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        actions = {e["path"]: e["action"] for e in data}
        assert actions["file2.txt"] == "added"

    def test_diff_nonexistent(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "diff", "nonexistent", "v1", "v2"]
        )
        assert result.exit_code == 1
