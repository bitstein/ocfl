import json
from pathlib import Path

from seed_root import SEED_ID
from typer.testing import CliRunner

from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app


class TestInfoRoot:
    def test_info_root(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "info"])
        assert result.exit_code == ExitCode.SUCCESS
        assert "1.1" in result.stdout

    def test_info_root_json(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "--json", "info"])
        assert result.exit_code == ExitCode.SUCCESS
        data = json.loads(result.stdout)
        assert data["spec"] == "1.1"
        assert "layout" in data


class TestInfoObject:
    def test_info_object(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "info", SEED_ID])
        assert result.exit_code == ExitCode.SUCCESS
        assert SEED_ID in result.stdout
        assert "v2" in result.stdout

    def test_info_object_json(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "--json", "info", SEED_ID]
        )
        assert result.exit_code == ExitCode.SUCCESS
        data = json.loads(result.stdout)
        assert data["id"] == SEED_ID
        assert data["head"] == "v2"
        assert data["num_versions"] == 2

    def test_info_nonexistent(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "info", "nonexistent"]
        )
        assert result.exit_code == ExitCode.GENERIC
