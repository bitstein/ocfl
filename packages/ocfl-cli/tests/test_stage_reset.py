"""Tests for ocfl stage reset."""

import pytest
from typer.testing import CliRunner

from ocfl_cli.main import app
from ocfl_cli.staging import StagingStore


@pytest.fixture
def runner():
    return CliRunner()


def _invoke(runner, storage_root, staging, *args):
    return runner.invoke(
        app,
        ["--root", str(storage_root), "stage", "--staging-root", str(staging), *args],
    )


class TestStageReset:
    def test_reset_clears_staging(self, runner, storage_root, tmp_path):
        staging = tmp_path / "staging"
        _invoke(runner, storage_root, staging, "new", "obj:new")

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        assert store.exists(root_path, "obj:new")

        result = _invoke(runner, storage_root, staging, "reset", "obj:new")
        assert result.exit_code == 0, result.output
        assert not store.exists(root_path, "obj:new")

    def test_reset_no_staging_warns(self, runner, storage_root, tmp_path):
        """Reset on non-existent staging warns and exits 0."""
        staging = tmp_path / "staging"
        result = _invoke(runner, storage_root, staging, "reset", "obj:nope")
        assert result.exit_code == 0
        # Should have a warning
        assert "no staging" in result.output.lower() or "warn" in result.output.lower()
