from pathlib import Path

from runner_helpers import ObjRunner
from seed_root import SEED_ID

from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app
from ocfl_lib import LocalFS, StorageRoot


class TestPurge:
    def test_purge_with_force(self, runner: ObjRunner, storage_root: Path):
        result = runner.invoke(
            app,
            ["--root", str(storage_root), "purge", SEED_ID, "--yes"],
        )
        assert result.exit_code == ExitCode.SUCCESS
        fs = LocalFS(storage_root)
        root = StorageRoot.open(fs)
        assert not root.get_object(SEED_ID).exists

    def test_purge_confirm_yes(self, runner: ObjRunner, storage_root: Path):
        result = runner.invoke(
            app,
            ["--root", str(storage_root), "purge", SEED_ID],
            input="y\n",
        )
        assert result.exit_code == ExitCode.SUCCESS
        fs = LocalFS(storage_root)
        root = StorageRoot.open(fs)
        assert not root.get_object(SEED_ID).exists

    def test_purge_confirm_no(self, runner: ObjRunner, storage_root: Path):
        result = runner.invoke(
            app,
            ["--root", str(storage_root), "purge", SEED_ID],
            input="n\n",
        )
        assert result.exit_code == ExitCode.GENERIC
        assert "Aborted" in result.output
        fs = LocalFS(storage_root)
        root = StorageRoot.open(fs)
        assert root.get_object(SEED_ID).exists

    def test_purge_nonexistent(self, runner: ObjRunner, storage_root: Path):
        result = runner.invoke(
            app,
            ["--root", str(storage_root), "purge", "nonexistent", "--yes"],
        )
        assert result.exit_code == ExitCode.GENERIC

    def test_purge_yes_skips_the_prompt(self, runner: ObjRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "purge", SEED_ID, "-y"]
        )
        assert result.exit_code == ExitCode.SUCCESS, result.output
        fs = LocalFS(storage_root)
        assert not StorageRoot.open(fs).get_object(SEED_ID).exists

    def test_force_is_a_hidden_alias(self, runner: ObjRunner) -> None:
        result = runner.invoke(app, ["purge", "--help"])
        assert "--yes" in result.output and "-y" in result.output
        assert "--force" not in result.output
