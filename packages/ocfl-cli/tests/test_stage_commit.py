"""Tests for ocfl stage commit."""

import threading
import time
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ocfl_cli.errors import LockTimeoutError
from ocfl_cli.main import app
from ocfl_cli.stage import service
from ocfl_cli.stage.store import StagingStore
from ocfl_lib import LocalFS, StorageRoot
from ocfl_lib.errors import StaleHeadError


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

        with patch(
            "ocfl_cli.stage.service.execute_plan", side_effect=KeyboardInterrupt
        ):
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

        with patch(
            "ocfl_cli.stage.service.execute_plan", side_effect=KeyboardInterrupt
        ):
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

        with patch(
            "ocfl_cli.stage.service.execute_plan", side_effect=KeyboardInterrupt
        ):
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
        with patch(
            "ocfl_cli.stage.service.execute_plan", side_effect=KeyboardInterrupt
        ):
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


class TestCrashRecovery:
    def test_generic_crash_then_retry_converges(self, runner, storage_root, tmp_path):
        """After a generic execution failure, rollback runs and retry succeeds."""
        oid = "obj:crash"
        staging = tmp_path / "staging"
        src = tmp_path / "file.txt"
        src.write_text("content")

        _invoke(runner, storage_root, staging, "new", oid)
        _invoke(runner, storage_root, staging, "add", oid, str(src))

        with (
            patch(
                "ocfl_cli.stage.service.execute_plan",
                side_effect=RuntimeError("simulated crash"),
            ),
            patch(
                "ocfl_cli.stage.service.revert_plan",
                wraps=service.revert_plan,
            ) as revert_spy,
        ):
            result = _invoke(runner, storage_root, staging, "commit", oid)

        assert result.exit_code == 1, result.output
        assert "simulated crash" in result.output
        assert revert_spy.call_count == 1

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        assert store.exists(root_path, oid)

        fs = LocalFS(storage_root)
        sr = StorageRoot(fs)
        assert not sr.get_object(oid).exists

        result = _invoke(runner, storage_root, staging, "commit", oid, "-m", "retry")
        assert result.exit_code == 0, result.output

        obj = sr.get_object(oid)
        assert obj.exists
        assert obj.head.number == 1
        assert not store.exists(root_path, oid)


class TestCommitLocking:
    def test_object_commit_lock_timeout_raises(self, tmp_path):
        """A contended object commit lock raises the canonical lock timeout."""
        root_path = str(tmp_path / "root")
        object_id = "obj:locked"

        with (
            service._object_commit_lock(root_path, object_id, timeout=1),
            pytest.raises(LockTimeoutError, match="commit lock"),
            service._object_commit_lock(root_path, object_id, timeout=0.01),
        ):
            pytest.fail("second lock acquire should time out")

    def test_bare_commits_for_same_object_are_serialized(self, tmp_path):
        """Local CLI commits for one object do not overlap plan execution."""
        current = 0
        peak = 0
        guard = threading.Lock()

        def fake_execute_plan(_plan):
            nonlocal current, peak
            with guard:
                current += 1
                peak = max(peak, current)
            time.sleep(0.05)
            with guard:
                current -= 1

        with (
            patch(
                "ocfl_cli.stage.service.build_inventory_plan",
                return_value=(object(), object()),
            ),
            patch("ocfl_cli.stage.service.prepare_steps"),
            patch("ocfl_cli.stage.service.execute_plan", side_effect=fake_execute_plan),
        ):
            threads = [
                threading.Thread(
                    target=service.commit_bare,
                    kwargs={
                        "store": StagingStore(tmp_path / "staging"),
                        "sr": object(),
                        "fs": object(),
                        "obj_dir": "obj",
                        "root_path": str(tmp_path / "root"),
                        "object_id": "obj:locked",
                        "message": None,
                        "user": None,
                        "created": None,
                    },
                    daemon=True,
                )
                for _ in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

        assert all(not thread.is_alive() for thread in threads)
        assert peak == 1

    def test_stale_head_preserves_staging_for_retry(
        self, runner, storage_root, tmp_path
    ):
        """A stale-head commit failure rolls back and leaves staging intact."""
        oid = "obj:stale"
        staging = tmp_path / "staging"
        src = tmp_path / "file.txt"
        src.write_text("content")

        _invoke(runner, storage_root, staging, "new", oid)
        _invoke(runner, storage_root, staging, "add", oid, str(src))

        stale = StaleHeadError(
            object_path="",
            expected_digest="a" * 128,
            actual_digest="b" * 128,
        )
        with (
            patch("ocfl_cli.stage.service.execute_plan", side_effect=stale),
            patch("ocfl_cli.stage.service.revert_plan", wraps=service.revert_plan),
        ):
            result = _invoke(runner, storage_root, staging, "commit", oid)

        assert result.exit_code == 1, result.output
        assert "stale" in result.output.lower()

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        assert store.exists(root_path, oid)
