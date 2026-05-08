"""Tests for staged version metadata CLI commands."""

from pathlib import Path
from typing import Any

import filelock
import pytest
from click.testing import Result
from typer.testing import CliRunner

from ocfl_cli.main import app
from ocfl_cli.stage import service
from ocfl_cli.stage.schemas import StagedMetadata, StagedUser
from ocfl_cli.stage.state import with_lock
from ocfl_cli.stage.store import StagingStore


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _invoke(runner: CliRunner, storage_root: Path, staging: Path, *args: str) -> Result:
    return runner.invoke(
        app,
        ["--root", str(storage_root), "stage", "--staging-root", str(staging), *args],
    )


def _metadata(storage_root: Path, staging: Path, object_id: str) -> StagedMetadata:
    store = StagingStore(staging)
    return store.load_metadata(str(storage_root.resolve()), object_id)


def _write_ots_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "ocfl-config" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        """\
[simpleproof]
base_url = "https://stage.example"
proof_category_id = 641

[bitcoin]
rpc_url = "http://127.0.0.1:8332"
zmq_endpoint = "tcp://127.0.0.1:28332"
"""
    )
    monkeypatch.setenv("OCFL_CONFIG", str(cfg))


class TestStageNewMetadata:
    def test_new_accepts_initial_metadata(
        self, runner: CliRunner, storage_root: Path, tmp_path: Path
    ) -> None:
        staging = tmp_path / "staging"

        result = _invoke(
            runner,
            storage_root,
            staging,
            "new",
            "obj:new",
            "-m",
            "Initial deposit",
            "--name",
            "Alice",
            "--address",
            "mailto:alice@example.org",
        )

        assert result.exit_code == 0, result.output
        assert _metadata(storage_root, staging, "obj:new") == StagedMetadata(
            message="Initial deposit",
            user=StagedUser(name="Alice", address="mailto:alice@example.org"),
        )

    def test_new_ots_accepts_initial_metadata(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_ots_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"

        result = _invoke(
            runner,
            storage_root,
            staging,
            "new",
            "obj:ots",
            "--ots",
            "-m",
            "Timestamped deposit",
            "--name",
            "Alice",
            "--address",
            "mailto:alice@example.org",
        )

        assert result.exit_code == 0, result.output
        assert _metadata(storage_root, staging, "obj:ots") == StagedMetadata(
            message="Timestamped deposit",
            user=StagedUser(name="Alice", address="mailto:alice@example.org"),
        )

    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            (("--message", " "), "message"),
            (("--name", " "), "name"),
            (("--address", " "), "address"),
        ],
    )
    def test_new_rejects_empty_metadata_value_without_creating_staging(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        args: tuple[str, ...],
        expected: str,
    ) -> None:
        staging = tmp_path / "staging"

        result = _invoke(
            runner,
            storage_root,
            staging,
            "new",
            "obj:new",
            *args,
        )

        assert result.exit_code == 2
        assert expected in result.output.lower()
        store = StagingStore(staging)
        assert not store.exists(str(storage_root.resolve()), "obj:new")


class TestStageMetadata:
    def test_metadata_updates_only_supplied_fields(
        self, runner: CliRunner, storage_root: Path, tmp_path: Path
    ) -> None:
        staging = tmp_path / "staging"
        assert (
            _invoke(
                runner,
                storage_root,
                staging,
                "new",
                "obj:new",
                "-m",
                "Initial",
                "--name",
                "Alice",
                "--address",
                "mailto:alice@example.org",
            ).exit_code
            == 0
        )

        result = _invoke(
            runner,
            storage_root,
            staging,
            "metadata",
            "obj:new",
            "--name",
            "Bob",
        )

        assert result.exit_code == 0, result.output
        assert _metadata(storage_root, staging, "obj:new") == StagedMetadata(
            message="Initial",
            user=StagedUser(name="Bob", address="mailto:alice@example.org"),
        )

    def test_metadata_clears_fields(
        self, runner: CliRunner, storage_root: Path, tmp_path: Path
    ) -> None:
        staging = tmp_path / "staging"
        assert (
            _invoke(
                runner,
                storage_root,
                staging,
                "new",
                "obj:new",
                "-m",
                "Initial",
                "--name",
                "Alice",
                "--address",
                "mailto:alice@example.org",
            ).exit_code
            == 0
        )

        result = _invoke(
            runner,
            storage_root,
            staging,
            "metadata",
            "obj:new",
            "--clear-message",
            "--clear-address",
        )

        assert result.exit_code == 0, result.output
        assert _metadata(storage_root, staging, "obj:new") == StagedMetadata(
            user=StagedUser(name="Alice")
        )

    def test_clear_name_also_clears_address_unless_address_is_set(
        self, runner: CliRunner, storage_root: Path, tmp_path: Path
    ) -> None:
        staging = tmp_path / "staging"
        assert (
            _invoke(
                runner,
                storage_root,
                staging,
                "new",
                "obj:new",
                "--name",
                "Alice",
                "--address",
                "mailto:alice@example.org",
            ).exit_code
            == 0
        )

        result = _invoke(
            runner,
            storage_root,
            staging,
            "metadata",
            "obj:new",
            "--clear-name",
            "--address",
            "mailto:bob@example.org",
        )

        assert result.exit_code == 0, result.output
        assert _metadata(storage_root, staging, "obj:new") == StagedMetadata(
            user=StagedUser(address="mailto:bob@example.org")
        )

        result = _invoke(
            runner,
            storage_root,
            staging,
            "metadata",
            "obj:new",
            "--clear-name",
        )

        assert result.exit_code == 0, result.output
        assert _metadata(storage_root, staging, "obj:new") == StagedMetadata()

    @pytest.mark.parametrize(
        "args",
        [
            ("--message", "updated", "--clear-message"),
            ("--name", "Bob", "--clear-name"),
            ("--address", "mailto:bob@example.org", "--clear-address"),
        ],
    )
    def test_metadata_rejects_set_and_clear_conflict(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        args: tuple[str, ...],
    ) -> None:
        staging = tmp_path / "staging"
        assert _invoke(runner, storage_root, staging, "new", "obj:new").exit_code == 0

        result = _invoke(runner, storage_root, staging, "metadata", "obj:new", *args)

        assert result.exit_code == 2
        assert "incompatible" in result.output.lower()

    def test_metadata_rejects_empty_value(
        self, runner: CliRunner, storage_root: Path, tmp_path: Path
    ) -> None:
        staging = tmp_path / "staging"
        assert _invoke(runner, storage_root, staging, "new", "obj:new").exit_code == 0

        result = _invoke(
            runner,
            storage_root,
            staging,
            "metadata",
            "obj:new",
            "--name",
            "\t",
        )

        assert result.exit_code == 2
        assert "name" in result.output.lower()

    def test_metadata_updates_staging_without_metadata_file(
        self, runner: CliRunner, storage_root: Path, tmp_path: Path
    ) -> None:
        staging = tmp_path / "staging"
        assert _invoke(runner, storage_root, staging, "new", "obj:new").exit_code == 0
        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        (store.staging_path(root_path, "obj:new") / "metadata.json").unlink()

        result = _invoke(
            runner,
            storage_root,
            staging,
            "metadata",
            "obj:new",
            "--message",
            "Recovered metadata",
        )

        assert result.exit_code == 0, result.output
        assert _metadata(storage_root, staging, "obj:new") == StagedMetadata(
            message="Recovered metadata"
        )

    def test_metadata_command_holds_staging_lock_during_update(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        staging = tmp_path / "staging"
        assert _invoke(runner, storage_root, staging, "new", "obj:new").exit_code == 0
        store = StagingStore(staging)
        staging_dir = store.staging_path(str(storage_root.resolve()), "obj:new")
        original_update_metadata = service.update_metadata
        observed_lock = False

        def assert_locked(*args: Any, **kwargs: Any) -> StagedMetadata:
            nonlocal observed_lock
            sibling = filelock.FileLock(str(staging_dir / ".lock"))
            with pytest.raises(filelock.Timeout):
                sibling.acquire(timeout=0)
            observed_lock = True
            return original_update_metadata(*args, **kwargs)

        monkeypatch.setattr(service, "update_metadata", assert_locked)

        result = _invoke(
            runner,
            storage_root,
            staging,
            "metadata",
            "obj:new",
            "--message",
            "Locked update",
        )

        assert result.exit_code == 0, result.output
        assert observed_lock

    def test_metadata_command_reports_lock_timeout(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        staging = tmp_path / "staging"
        assert _invoke(runner, storage_root, staging, "new", "obj:new").exit_code == 0
        store = StagingStore(staging)
        staging_dir = store.staging_path(str(storage_root.resolve()), "obj:new")
        monkeypatch.setattr(service, "_LOCK_TIMEOUT_SECONDS", 0.01)

        with with_lock(staging_dir, timeout=1):
            result = _invoke(
                runner,
                storage_root,
                staging,
                "metadata",
                "obj:new",
                "--message",
                "Blocked update",
            )

        assert result.exit_code == 4
        assert "lock" in result.output.lower()

    def test_metadata_usage_error_precedes_lock_timeout(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        staging = tmp_path / "staging"
        assert _invoke(runner, storage_root, staging, "new", "obj:new").exit_code == 0
        store = StagingStore(staging)
        staging_dir = store.staging_path(str(storage_root.resolve()), "obj:new")
        monkeypatch.setattr(service, "_LOCK_TIMEOUT_SECONDS", 0.01)

        with with_lock(staging_dir, timeout=1):
            result = _invoke(
                runner,
                storage_root,
                staging,
                "metadata",
                "obj:new",
                "--name",
                "Bob",
                "--clear-name",
            )

        assert result.exit_code == 2
        assert "incompatible" in result.output.lower()
