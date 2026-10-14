import json
from pathlib import Path

from typer.testing import CliRunner

from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app


class TestLog:
    def test_log(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "log", "obj:test"])
        assert result.exit_code == ExitCode.SUCCESS
        assert "v1" in result.stdout
        assert "v2" in result.stdout

    def test_log_reverse(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "log", "obj:test", "--reverse"]
        )
        assert result.exit_code == ExitCode.SUCCESS
        lines = result.stdout.strip().splitlines()
        v1_idx = next(i for i, line in enumerate(lines) if "v1" in line)
        v2_idx = next(i for i, line in enumerate(lines) if "v2" in line)
        assert v1_idx < v2_idx

    def test_log_limit(self, runner: CliRunner, storage_root: Path):
        for flag in ("-n", "--limit"):
            result = runner.invoke(
                app, ["--root", str(storage_root), "log", "obj:test", flag, "1"]
            )
            assert result.exit_code == ExitCode.SUCCESS
            assert "v2" in result.stdout
            assert "v1" not in result.stdout

    def test_log_json(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "--json", "log", "obj:test"]
        )
        assert result.exit_code == ExitCode.SUCCESS
        data = json.loads(result.stdout)
        assert len(data) == 2
        assert data[0]["version"] == "v2"
        assert data[0]["message"] == "v2"

    def test_log_nonexistent(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "log", "nonexistent"])
        assert result.exit_code == ExitCode.GENERIC
