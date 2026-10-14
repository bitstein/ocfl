import json
from pathlib import Path

from typer.testing import CliRunner

from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app


class TestShow:
    def test_show_head(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "show", "obj:test"])
        assert result.exit_code == ExitCode.SUCCESS
        assert "v2" in result.stdout
        assert "file2.txt" in result.stdout

    def test_show_v1(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "show", "obj:test", "v1"]
        )
        assert result.exit_code == ExitCode.SUCCESS
        assert "v1" in result.stdout
        assert "file1.txt" in result.stdout

    def test_show_bare_version(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "show", "obj:test", "1"]
        )
        assert result.exit_code == ExitCode.SUCCESS
        assert "v1" in result.stdout

    def test_nonexistent_version_reports_clean_error(
        self, runner: CliRunner, storage_root: Path
    ):
        result = runner.invoke(
            app, ["--root", str(storage_root), "show", "obj:test", "v9"]
        )
        assert result.exit_code == ExitCode.GENERIC
        assert "not found" in result.stderr.lower()
        assert result.exception.__class__.__name__ == "SystemExit"

    def test_show_json(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "--json", "show", "obj:test"]
        )
        assert result.exit_code == ExitCode.SUCCESS
        data = json.loads(result.stdout)
        assert data["version"] == "v2"
        assert "changes" in data
        assert data["message"] == "v2"

    def test_show_v1_json(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "--json", "show", "obj:test", "v1"]
        )
        assert result.exit_code == ExitCode.SUCCESS
        data = json.loads(result.stdout)
        assert data["version"] == "v1"
        assert all(c["action"] == "added" for c in data["changes"])

    def test_show_nonexistent(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "show", "nonexistent"]
        )
        assert result.exit_code == ExitCode.GENERIC
