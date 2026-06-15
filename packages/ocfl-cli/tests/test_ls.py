"""Tests for ocfl ls command."""

import json
from pathlib import Path

from typer.testing import CliRunner

from ocfl_cli.main import app


class TestLsRoot:
    def test_list_objects(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "ls"])
        assert result.exit_code == 0
        assert "obj:test" in result.stdout

    def test_list_objects_json(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "--json", "ls"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "obj:test" in data

    def test_empty_root(self, runner: CliRunner, tmp_path: Path):
        """Empty root (no objects) should still succeed."""
        from ocfl_lib import LocalFS, Spec, StorageRoot, get_layout

        fs = LocalFS(tmp_path)
        StorageRoot.init(
            fs, fs, spec=Spec.V1_1, layout=get_layout("0002-flat-direct-storage-layout")
        )
        result = runner.invoke(app, ["--root", str(tmp_path), "ls"])
        assert result.exit_code == 0


class TestLsObject:
    def test_list_files_head(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "ls", "obj:test"])
        assert result.exit_code == 0
        assert "file1.txt" in result.stdout
        assert "file2.txt" in result.stdout

    def test_list_files_version(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "ls", "obj:test", "--version", "v1"]
        )
        assert result.exit_code == 0
        assert "file1.txt" in result.stdout
        assert "file2.txt" not in result.stdout

    def test_list_files_bare_version(self, runner: CliRunner, storage_root: Path):
        """Accept '1' as well as 'v1'."""
        result = runner.invoke(
            app, ["--root", str(storage_root), "ls", "obj:test", "--version", "1"]
        )
        assert result.exit_code == 0
        assert "file1.txt" in result.stdout

    def test_nonexistent_version_reports_clean_error(
        self, runner: CliRunner, storage_root: Path
    ):
        result = runner.invoke(
            app, ["--root", str(storage_root), "ls", "obj:test", "--version", "v9"]
        )
        assert result.exit_code == 1
        assert "not found" in result.stderr.lower()
        assert result.exception.__class__.__name__ == "SystemExit"

    def test_list_files_json(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "--json", "ls", "obj:test"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        paths = [f["path"] for f in data]
        assert "file1.txt" in paths
        assert "file2.txt" in paths

    def test_list_files_digests(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "ls", "obj:test", "--digests"]
        )
        assert result.exit_code == 0
        # Should contain digest hashes
        assert "file1.txt" in result.stdout
        lines = result.stdout.strip().splitlines()
        assert any(len(line.split()) >= 2 for line in lines)

    def test_nonexistent_object(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "ls", "nonexistent"])
        assert result.exit_code == 1
