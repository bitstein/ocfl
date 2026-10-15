import json
from pathlib import Path

from runner_helpers import ObjRunner
from seed_root import SEED_ID

from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app


class TestDiff:
    def test_diff(self, runner: ObjRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "diff", SEED_ID, "v1", "v2"]
        )
        assert result.exit_code == ExitCode.SUCCESS
        assert "file2.txt" in result.stdout
        assert "A  file2.txt" in result.stdout

    def test_diff_bare_versions(self, runner: ObjRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "diff", SEED_ID, "1", "2"]
        )
        assert result.exit_code == ExitCode.SUCCESS
        assert "file2.txt" in result.stdout

    def test_nonexistent_version_reports_clean_error(
        self, runner: ObjRunner, storage_root: Path
    ):
        result = runner.invoke(
            app, ["--root", str(storage_root), "diff", SEED_ID, "v1", "v9"]
        )
        assert result.exit_code == ExitCode.GENERIC
        assert "not found" in result.stderr.lower()
        assert result.exception.__class__.__name__ == "SystemExit"

    def test_diff_json(self, runner: ObjRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "--json", "diff", SEED_ID, "v1", "v2"]
        )
        assert result.exit_code == ExitCode.SUCCESS
        data = json.loads(result.stdout)
        actions = {e["path"]: e["action"] for e in data}
        assert actions["file2.txt"] == "added"

    def test_diff_nonexistent(self, runner: ObjRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "diff", "nonexistent", "v1", "v2"]
        )
        assert result.exit_code == ExitCode.GENERIC
