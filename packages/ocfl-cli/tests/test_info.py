"""Tests for ocfl info command."""

import json
from pathlib import Path

from typer.testing import CliRunner

from ocfl_cli.main import app


class TestInfoRoot:
    def test_info_root(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "info"])
        assert result.exit_code == 0
        assert "1.1" in result.stdout

    def test_info_root_json(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "--json", "info"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["spec"] == "1.1"
        assert "layout" in data


class TestInfoObject:
    def test_info_object(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "info", "obj:test"])
        assert result.exit_code == 0
        assert "obj:test" in result.stdout
        assert "v2" in result.stdout

    def test_info_object_json(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "--json", "info", "obj:test"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["id"] == "obj:test"
        assert data["head"] == "v2"
        assert data["num_versions"] == 2

    def test_info_nonexistent(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "info", "nonexistent"]
        )
        assert result.exit_code == 1
