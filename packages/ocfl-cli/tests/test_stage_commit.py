"""Tests for ocfl stage commit."""

import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import filelock
import pytest
from typer.testing import CliRunner

from ocfl_cli.errors import LockTimeoutError
from ocfl_cli.main import app
from ocfl_cli.store import locks as locks
from ocfl_cli.store.staging import StagingStore
from ocfl_cli.workflow import commit as wf_commit
from ocfl_cli.workflow import common as wf_common
from ocfl_lib import LocalFS, StaleHeadError, StorageRoot

# Content lookups run mid-commit, after the commit has started writing.
_FAIL_DURING_COMMIT = "ocfl_cli.workflow.commit._StagingContentSource.get_content"


@pytest.fixture
def runner():
    return CliRunner()


def _invoke(runner, storage_root, staging, *args):
    return runner.invoke(
        app,
        ["--root", str(storage_root), "stage", "--staging-root", str(staging), *args],
    )


def _new_with_metadata(runner, storage_root, staging, object_id):
    return _invoke(
        runner,
        storage_root,
        staging,
        "new",
        object_id,
        "-m",
        "staged version",
        "--name",
        "Alice",
        "--address",
        "mailto:alice@example.org",
    )


class TestStageCommit:
    def test_full_workflow(self, runner, storage_root, tmp_path):
        """stage new → stage add → stage commit produces valid object."""
        staging = tmp_path / "staging"
        src = tmp_path / "hello.txt"
        src.write_text("hello world")

        _new_with_metadata(runner, storage_root, staging, "obj:committed")
        _invoke(runner, storage_root, staging, "add", "obj:committed", str(src))
        result = _invoke(runner, storage_root, staging, "commit", "obj:committed")
        assert result.exit_code == 0, result.output

        # Verify object exists in storage root
        fs = LocalFS(storage_root)
        sr = StorageRoot.open(fs)
        obj = sr.get_object("obj:committed")
        assert obj.exists
        assert obj.head.number == 1
        vi = obj.version_info(obj.head)
        assert vi.message == "staged version"
        assert vi.user is not None
        assert vi.user.name == "Alice"
        assert vi.user.address == "mailto:alice@example.org"

        # Verify staging was purged
        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        assert not store.exists(root_path, "obj:committed")

    def test_commit_existing_object(self, runner, storage_root, tmp_path):
        """Stage new version for existing object and commit."""
        staging = tmp_path / "staging"
        src = tmp_path / "extra.txt"
        src.write_text("extra content")

        _new_with_metadata(runner, storage_root, staging, "obj:test")
        _invoke(runner, storage_root, staging, "add", "obj:test", str(src))
        result = _invoke(runner, storage_root, staging, "commit", "obj:test")
        assert result.exit_code == 0, result.output

        fs = LocalFS(storage_root)
        sr = StorageRoot.open(fs)
        obj = sr.get_object("obj:test")
        assert obj.head.number == 3  # was v2, now v3

    def test_stale_file_detected(self, runner, storage_root, tmp_path):
        """Modified source file after staging is detected."""
        staging = tmp_path / "staging"
        src = tmp_path / "data.txt"
        src.write_text("original")

        _new_with_metadata(runner, storage_root, staging, "obj:stale")
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

        _new_with_metadata(runner, storage_root, staging, "obj:missing")
        _invoke(runner, storage_root, staging, "add", "obj:missing", str(src))

        src.unlink()

        result = _invoke(runner, storage_root, staging, "commit", "obj:missing")
        assert result.exit_code == 1

        # Staging preserved
        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        assert store.exists(root_path, "obj:missing")

    def test_commit_with_user(self, runner, storage_root, tmp_path):
        """Commit uses staged user metadata."""
        staging = tmp_path / "staging"
        src = tmp_path / "file.txt"
        src.write_text("content")

        _new_with_metadata(runner, storage_root, staging, "obj:user")
        _invoke(runner, storage_root, staging, "add", "obj:user", str(src))
        result = _invoke(runner, storage_root, staging, "commit", "obj:user")
        assert result.exit_code == 0, result.output

        fs = LocalFS(storage_root)
        sr = StorageRoot.open(fs)
        obj = sr.get_object("obj:user")
        vi = obj.version_info(obj.head)
        assert vi.user is not None
        assert vi.user.name == "Alice"
        assert vi.user.address == "mailto:alice@example.org"

    def test_commit_uses_metadata_command_updates(self, runner, storage_root, tmp_path):
        staging = tmp_path / "staging"
        src = tmp_path / "file.txt"
        src.write_text("content")

        _invoke(runner, storage_root, staging, "new", "obj:metadata-command")
        _invoke(
            runner,
            storage_root,
            staging,
            "metadata",
            "obj:metadata-command",
            "-m",
            "reviewed metadata",
            "--name",
            "Bob",
            "--address",
            "mailto:bob@example.org",
        )
        _invoke(runner, storage_root, staging, "add", "obj:metadata-command", str(src))

        result = _invoke(
            runner, storage_root, staging, "commit", "obj:metadata-command"
        )

        assert result.exit_code == 0, result.output
        obj = StorageRoot.open(LocalFS(storage_root)).get_object("obj:metadata-command")
        vi = obj.version_info(obj.head)
        assert vi.message == "reviewed metadata"
        assert vi.user is not None
        assert vi.user.name == "Bob"
        assert vi.user.address == "mailto:bob@example.org"

    def test_missing_staged_metadata_blocks_commit(
        self, runner, storage_root, tmp_path
    ):
        staging = tmp_path / "staging"
        src = tmp_path / "file.txt"
        src.write_text("content")

        _invoke(runner, storage_root, staging, "new", "obj:missing-meta")
        _invoke(runner, storage_root, staging, "add", "obj:missing-meta", str(src))

        result = _invoke(runner, storage_root, staging, "commit", "obj:missing-meta")

        assert result.exit_code == 1
        assert "missing staged metadata" in result.output.lower()
        assert "message" in result.output
        assert "user.name" in result.output
        assert "user.address" in result.output

    def test_allow_missing_metadata_bypasses_metadata_gate(
        self, runner, storage_root, tmp_path
    ):
        staging = tmp_path / "staging"
        src = tmp_path / "file.txt"
        src.write_text("content")

        _invoke(runner, storage_root, staging, "new", "obj:allow-meta")
        _invoke(runner, storage_root, staging, "add", "obj:allow-meta", str(src))

        result = _invoke(
            runner,
            storage_root,
            staging,
            "commit",
            "obj:allow-meta",
            "--allow-missing-metadata",
        )

        assert result.exit_code == 0, result.output

    @pytest.mark.parametrize(
        "args",
        [
            ("-m", "late metadata"),
            ("--message", "late metadata"),
            ("--name", "Alice"),
            ("--address", "mailto:alice@example.org"),
            ("--created", "2026-05-08T00:00:00Z"),
        ],
    )
    def test_commit_rejects_removed_commit_time_metadata_options(
        self, runner, storage_root, tmp_path, args
    ):
        staging = tmp_path / "staging"
        assert (
            _new_with_metadata(
                runner, storage_root, staging, "obj:no-commit-metadata"
            ).exit_code
            == 0
        )

        result = _invoke(
            runner,
            storage_root,
            staging,
            "commit",
            "obj:no-commit-metadata",
            *args,
        )

        assert result.exit_code == 2
        assert "no such option" in result.output.lower()

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

        _new_with_metadata(runner, storage_root, staging, "obj:interrupted")
        _invoke(runner, storage_root, staging, "add", "obj:interrupted", str(src))

        with patch(_FAIL_DURING_COMMIT, side_effect=KeyboardInterrupt):
            result = _invoke(runner, storage_root, staging, "commit", "obj:interrupted")

        assert result.exit_code == 130
        assert "interrupted" in result.output.lower()

    def test_keyboard_interrupt_preserves_staging(self, runner, storage_root, tmp_path):
        """Staging survives KeyboardInterrupt for retry."""
        staging = tmp_path / "staging"
        src = tmp_path / "file.txt"
        src.write_text("content")

        _new_with_metadata(runner, storage_root, staging, "obj:retry")
        _invoke(runner, storage_root, staging, "add", "obj:retry", str(src))

        with patch(_FAIL_DURING_COMMIT, side_effect=KeyboardInterrupt):
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

        _new_with_metadata(runner, storage_root, staging, "obj:clean")
        _invoke(runner, storage_root, staging, "add", "obj:clean", str(src))

        with patch(_FAIL_DURING_COMMIT, side_effect=KeyboardInterrupt):
            _invoke(runner, storage_root, staging, "commit", "obj:clean")

        # Object should not exist (new object, fully reverted)
        fs = LocalFS(storage_root)
        sr = StorageRoot.open(fs)
        assert not sr.get_object("obj:clean").exists

    def test_retry_after_interrupt(self, runner, storage_root, tmp_path):
        """After interrupted commit, retry succeeds."""
        staging = tmp_path / "staging"
        src = tmp_path / "file.txt"
        src.write_text("content")

        _new_with_metadata(runner, storage_root, staging, "obj:retried")
        _invoke(runner, storage_root, staging, "add", "obj:retried", str(src))

        # First attempt: interrupted
        with patch(_FAIL_DURING_COMMIT, side_effect=KeyboardInterrupt):
            _invoke(runner, storage_root, staging, "commit", "obj:retried")

        # Retry succeeds
        result = _invoke(runner, storage_root, staging, "commit", "obj:retried")
        assert result.exit_code == 0, result.output

        fs = LocalFS(storage_root)
        sr = StorageRoot.open(fs)
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

        _new_with_metadata(runner, storage_root, staging, oid)
        _invoke(runner, storage_root, staging, "add", oid, str(src))

        with patch(
            _FAIL_DURING_COMMIT,
            side_effect=RuntimeError("simulated crash"),
        ):
            result = _invoke(runner, storage_root, staging, "commit", oid)

        assert result.exit_code == 1, result.output
        assert "simulated crash" in result.output

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        assert store.exists(root_path, oid)

        fs = LocalFS(storage_root)
        sr = StorageRoot.open(fs)
        assert not sr.get_object(oid).exists

        result = _invoke(runner, storage_root, staging, "commit", oid)
        assert result.exit_code == 0, result.output

        obj = sr.get_object(oid)
        assert obj.exists
        assert obj.head.number == 1
        assert not store.exists(root_path, oid)


class TestClaimLost:
    def test_lost_claim_exits_3_and_keeps_staging(self, runner, storage_root, tmp_path):
        """Another writer claims the object mid-commit: exit 3, nothing written."""
        oid = "obj:race"
        staging = tmp_path / "staging"
        src = tmp_path / "file.txt"
        src.write_text("content")
        _new_with_metadata(runner, storage_root, staging, oid)
        _invoke(runner, storage_root, staging, "add", oid, str(src))

        original = wf_commit._StagingContentSource.get_content

        def other_writer_claims_first(self, digest):
            (storage_root / oid).mkdir(exist_ok=True)
            return original(self, digest)

        with patch.object(
            wf_commit._StagingContentSource, "get_content", other_writer_claims_first
        ):
            result = _invoke(runner, storage_root, staging, "commit", oid)

        assert result.exit_code == 3, result.output
        assert "claimed" in result.output
        assert not any((storage_root / oid).iterdir())
        store = StagingStore(staging)
        assert store.exists(str(storage_root.resolve()), oid)


class TestCommitLocking:
    def test_object_commit_lock_timeout_raises(self, tmp_path):
        """A contended object commit lock raises the canonical lock timeout."""
        root_path = str(tmp_path / "root")
        object_id = "obj:locked"

        with (
            locks.object_commit_lock(root_path, object_id, timeout=1),
            pytest.raises(LockTimeoutError, match="commit lock"),
            locks.object_commit_lock(root_path, object_id, timeout=0.01),
        ):
            pytest.fail("second lock acquire should time out")

    def test_bare_commits_for_same_object_are_serialized(self, tmp_path):
        """Local CLI commits for one object do not overlap plan execution."""
        current = 0
        peak = 0
        guard = threading.Lock()

        def fake_commit(*_args, **_kwargs):
            nonlocal current, peak
            with guard:
                current += 1
                peak = max(peak, current)
            time.sleep(0.05)
            with guard:
                current -= 1

        store = StagingStore(tmp_path / "staging")
        root_path = str(tmp_path / "root")
        object_id = "obj:locked"
        staging_dir = store.staging_path(root_path, object_id)
        staging_dir.mkdir(parents=True)

        with patch(
            "ocfl_cli.workflow.commit.build_inventory_plan",
            return_value=(SimpleNamespace(object_id=object_id), object()),
        ):
            threads = [
                threading.Thread(
                    target=wf_commit.commit_bare,
                    kwargs={
                        "store": StagingStore(tmp_path / "staging"),
                        "sr": SimpleNamespace(commit=fake_commit),
                        "fs": object(),
                        "root_path": root_path,
                        "object_id": object_id,
                        "staging_dir": staging_dir,
                        "allow_missing_metadata": True,
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

    def test_bare_commit_holds_staging_lock_until_purge(
        self, runner, storage_root, tmp_path, monkeypatch
    ):
        oid = "obj:locked-metadata"
        staging = tmp_path / "staging"
        src = tmp_path / "file.txt"
        src.write_text("content")

        _new_with_metadata(runner, storage_root, staging, oid)
        _invoke(runner, storage_root, staging, "add", oid, str(src))

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        staging_dir = store.staging_path(root_path, oid)
        original_purge = StagingStore.purge
        observed_lock = False

        def assert_locked_during_purge(self, root_path_arg, object_id_arg):
            nonlocal observed_lock
            sibling = filelock.FileLock(str(staging_dir / ".lock"))
            with pytest.raises(filelock.Timeout):
                sibling.acquire(timeout=0)
            observed_lock = True
            return original_purge(self, root_path_arg, object_id_arg)

        monkeypatch.setattr(StagingStore, "purge", assert_locked_during_purge)

        result = _invoke(runner, storage_root, staging, "commit", oid)

        assert result.exit_code == 0, result.output
        assert observed_lock
        assert not staging_dir.exists()

    def test_bare_commit_reports_staging_lock_timeout(
        self, runner, storage_root, tmp_path, monkeypatch
    ):
        oid = "obj:lock-timeout"
        staging = tmp_path / "staging"
        src = tmp_path / "file.txt"
        src.write_text("content")

        _new_with_metadata(runner, storage_root, staging, oid)
        _invoke(runner, storage_root, staging, "add", oid, str(src))

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        staging_dir = store.staging_path(root_path, oid)
        monkeypatch.setattr(wf_common, "LOCK_TIMEOUT_SECONDS", 0.01)

        with filelock.FileLock(str(staging_dir / ".lock")).acquire(timeout=1):
            result = _invoke(runner, storage_root, staging, "commit", oid)

        assert result.exit_code == 4
        assert "lock" in result.output.lower()
        assert store.exists(root_path, oid)

    def test_stale_head_preserves_staging_for_retry(
        self, runner, storage_root, tmp_path
    ):
        """A stale-head commit failure rolls back and leaves staging intact."""
        oid = "obj:stale"
        staging = tmp_path / "staging"
        src = tmp_path / "file.txt"
        src.write_text("content")

        _new_with_metadata(runner, storage_root, staging, oid)
        _invoke(runner, storage_root, staging, "add", oid, str(src))

        stale = StaleHeadError(
            object_path="",
            expected_digest="a" * 128,
            actual_digest="b" * 128,
        )
        with patch(_FAIL_DURING_COMMIT, side_effect=stale):
            result = _invoke(runner, storage_root, staging, "commit", oid)

        assert result.exit_code == 1, result.output
        assert "stale" in result.output.lower()

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        assert store.exists(root_path, oid)
