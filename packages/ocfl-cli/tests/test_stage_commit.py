"""Tests for ocfl stage commit."""

import time
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ocfl_cli.main import app
from ocfl_cli.staging import StagingStore
from ocfl_lib import LocalFS, StorageRoot


@pytest.fixture
def runner():
    return CliRunner()


def _invoke(runner, storage_root, staging, *args):
    return runner.invoke(
        app,
        ["--root", str(storage_root), "stage", "--staging-root", str(staging), *args],
    )


class TestStageCommit:
    def test_full_workflow(self, runner, storage_root, tmp_path):
        """stage new → stage add → stage commit produces valid object."""
        staging = tmp_path / "staging"
        src = tmp_path / "hello.txt"
        src.write_text("hello world")

        _invoke(runner, storage_root, staging, "new", "obj:committed")
        _invoke(runner, storage_root, staging, "add", "obj:committed", str(src))
        result = _invoke(
            runner,
            storage_root,
            staging,
            "commit",
            "obj:committed",
            "-m",
            "first version",
        )
        assert result.exit_code == 0, result.output

        # Verify object exists in storage root
        fs = LocalFS(storage_root)
        sr = StorageRoot(fs)
        obj = sr.get_object("obj:committed")
        assert obj.exists
        assert obj.head.number == 1

        # Verify staging was purged
        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        assert not store.exists(root_path, "obj:committed")

    def test_commit_existing_object(self, runner, storage_root, tmp_path):
        """Stage new version for existing object and commit."""
        staging = tmp_path / "staging"
        src = tmp_path / "extra.txt"
        src.write_text("extra content")

        _invoke(runner, storage_root, staging, "new", "obj:test")
        _invoke(runner, storage_root, staging, "add", "obj:test", str(src))
        result = _invoke(
            runner,
            storage_root,
            staging,
            "commit",
            "obj:test",
            "-m",
            "v3",
        )
        assert result.exit_code == 0, result.output

        fs = LocalFS(storage_root)
        sr = StorageRoot(fs)
        obj = sr.get_object("obj:test")
        assert obj.head.number == 3  # was v2, now v3

    def test_stale_file_detected(self, runner, storage_root, tmp_path):
        """Modified source file after staging is detected."""
        staging = tmp_path / "staging"
        src = tmp_path / "data.txt"
        src.write_text("original")

        _invoke(runner, storage_root, staging, "new", "obj:stale")
        _invoke(runner, storage_root, staging, "add", "obj:stale", str(src))

        # Modify file after staging (change content but preserve path)
        time.sleep(0.05)  # ensure mtime changes
        src.write_text("modified content")

        result = _invoke(runner, storage_root, staging, "commit", "obj:stale")
        assert result.exit_code == 1
        assert "stale" in result.output.lower() or "changed" in result.output.lower()

        # Staging should be preserved for retry
        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        assert store.exists(root_path, "obj:stale")

    def test_missing_file_detected(self, runner, storage_root, tmp_path):
        """Deleted source file after staging is detected."""
        staging = tmp_path / "staging"
        src = tmp_path / "gone.txt"
        src.write_text("will be deleted")

        _invoke(runner, storage_root, staging, "new", "obj:missing")
        _invoke(runner, storage_root, staging, "add", "obj:missing", str(src))

        src.unlink()

        result = _invoke(runner, storage_root, staging, "commit", "obj:missing")
        assert result.exit_code == 1

        # Staging preserved
        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        assert store.exists(root_path, "obj:missing")

    def test_commit_with_user(self, runner, storage_root, tmp_path):
        """Commit with --name and --address."""
        staging = tmp_path / "staging"
        src = tmp_path / "file.txt"
        src.write_text("content")

        _invoke(runner, storage_root, staging, "new", "obj:user")
        _invoke(runner, storage_root, staging, "add", "obj:user", str(src))
        result = _invoke(
            runner,
            storage_root,
            staging,
            "commit",
            "obj:user",
            "-m",
            "with user",
            "--name",
            "Alice",
            "--address",
            "mailto:alice@example.org",
        )
        assert result.exit_code == 0, result.output

        fs = LocalFS(storage_root)
        sr = StorageRoot(fs)
        obj = sr.get_object("obj:user")
        vi = obj.version_info(obj.head)
        assert vi.user is not None
        assert vi.user.name == "Alice"
        assert vi.user.address == "mailto:alice@example.org"

    def test_no_staging_errors(self, runner, storage_root, tmp_path):
        staging = tmp_path / "staging"
        result = _invoke(runner, storage_root, staging, "commit", "obj:nope")
        assert result.exit_code == 1


class TestSignalHandling:
    def test_keyboard_interrupt_shows_message(self, runner, storage_root, tmp_path):
        """KeyboardInterrupt during commit shows feedback."""
        staging = tmp_path / "staging"
        src = tmp_path / "file.txt"
        src.write_text("content")

        _invoke(runner, storage_root, staging, "new", "obj:interrupted")
        _invoke(runner, storage_root, staging, "add", "obj:interrupted", str(src))

        with patch("ocfl_cli.commands.stage.execute_plan", side_effect=KeyboardInterrupt):
            result = _invoke(runner, storage_root, staging, "commit", "obj:interrupted")

        assert result.exit_code == 130
        assert "interrupted" in result.output.lower()

    def test_keyboard_interrupt_preserves_staging(self, runner, storage_root, tmp_path):
        """Staging survives KeyboardInterrupt for retry."""
        staging = tmp_path / "staging"
        src = tmp_path / "file.txt"
        src.write_text("content")

        _invoke(runner, storage_root, staging, "new", "obj:retry")
        _invoke(runner, storage_root, staging, "add", "obj:retry", str(src))

        with patch("ocfl_cli.commands.stage.execute_plan", side_effect=KeyboardInterrupt):
            _invoke(runner, storage_root, staging, "commit", "obj:retry")

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        assert store.exists(root_path, "obj:retry")

    def test_keyboard_interrupt_leaves_object_clean(
        self, runner, storage_root, tmp_path
    ):
        """Object state is clean after interrupted commit (no partial writes)."""
        staging = tmp_path / "staging"
        src = tmp_path / "file.txt"
        src.write_text("content")

        _invoke(runner, storage_root, staging, "new", "obj:clean")
        _invoke(runner, storage_root, staging, "add", "obj:clean", str(src))

        with patch("ocfl_cli.commands.stage.execute_plan", side_effect=KeyboardInterrupt):
            _invoke(runner, storage_root, staging, "commit", "obj:clean")

        # Object should not exist (new object, fully reverted)
        fs = LocalFS(storage_root)
        sr = StorageRoot(fs)
        assert not sr.get_object("obj:clean").exists

    def test_retry_after_interrupt(self, runner, storage_root, tmp_path):
        """After interrupted commit, retry succeeds."""
        staging = tmp_path / "staging"
        src = tmp_path / "file.txt"
        src.write_text("content")

        _invoke(runner, storage_root, staging, "new", "obj:retried")
        _invoke(runner, storage_root, staging, "add", "obj:retried", str(src))

        # First attempt: interrupted
        with patch("ocfl_cli.commands.stage.execute_plan", side_effect=KeyboardInterrupt):
            _invoke(runner, storage_root, staging, "commit", "obj:retried")

        # Retry succeeds
        result = _invoke(
            runner, storage_root, staging, "commit", "obj:retried", "-m", "retry"
        )
        assert result.exit_code == 0, result.output

        fs = LocalFS(storage_root)
        sr = StorageRoot(fs)
        obj = sr.get_object("obj:retried")
        assert obj.exists
        assert obj.head.number == 1
