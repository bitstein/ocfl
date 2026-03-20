"""Tests for ocfl purge command."""

from pathlib import Path

from typer.testing import CliRunner

from ocfl_cli.main import app
from ocfl_lib import LocalFS, StorageRoot


class TestPurge:
    def test_purge_with_force(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app,
            ["--root", str(storage_root), "purge", "obj:test", "--force"],
        )
        assert result.exit_code == 0
        # Object should be gone
        fs = LocalFS(storage_root)
        root = StorageRoot(fs)
        assert not root.get_object("obj:test").exists

    def test_purge_confirm_yes(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app,
            ["--root", str(storage_root), "purge", "obj:test"],
            input="y\n",
        )
        assert result.exit_code == 0
        fs = LocalFS(storage_root)
        root = StorageRoot(fs)
        assert not root.get_object("obj:test").exists

    def test_purge_confirm_no(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app,
            ["--root", str(storage_root), "purge", "obj:test"],
            input="n\n",
        )
        assert result.exit_code == 1
        assert "Aborted" in result.output
        # Object should still exist
        fs = LocalFS(storage_root)
        root = StorageRoot(fs)
        assert root.get_object("obj:test").exists

    def test_purge_nonexistent(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app,
            ["--root", str(storage_root), "purge", "nonexistent", "--force"],
        )
        assert result.exit_code == 1
