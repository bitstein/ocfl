"""Tests for ocfl stage new."""

import pytest
from typer.testing import CliRunner

from ocfl_cli.main import app


@pytest.fixture
def runner():
    return CliRunner()


class TestStageNew:
    def test_new_fresh_object(self, runner, storage_root, tmp_path):
        """New staging for object that doesn't exist in root."""
        staging = tmp_path / "staging"
        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "stage",
                "--staging-root",
                str(staging),
                "new",
                "obj:new",
            ],
        )
        assert result.exit_code == 0, result.output

        # Verify staging dir created with v1 inventory
        from ocfl_cli.staging import StagingStore

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        assert store.exists(root_path, "obj:new")
        inv = store.load_inventory(root_path, "obj:new")
        assert inv.id == "obj:new"
        assert inv.head == "v1"
        assert inv.manifest == {}
        assert inv.versions["v1"].state == {}

    def test_new_existing_object(self, runner, storage_root, tmp_path):
        """New staging for object that already exists — clones head, adds v(N+1)."""
        staging = tmp_path / "staging"
        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "stage",
                "--staging-root",
                str(staging),
                "new",
                "obj:test",
            ],
        )
        assert result.exit_code == 0, result.output

        from ocfl_cli.staging import StagingStore

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        inv = store.load_inventory(root_path, "obj:test")
        # Existing object has v2 head, so staged should be v3
        assert inv.head == "v3"
        # Prior manifest should be preserved
        assert len(inv.manifest) > 0
        # New version should have empty state (placeholder)
        assert inv.versions["v3"].state == {}

    def test_error_on_double_new(self, runner, storage_root, tmp_path):
        """Error if staging already exists for this (root, object_id)."""
        staging = tmp_path / "staging"
        args = [
            "--root",
            str(storage_root),
            "stage",
            "--staging-root",
            str(staging),
            "new",
            "obj:new",
        ]
        result = runner.invoke(app, args)
        assert result.exit_code == 0
        # Second time should fail
        result = runner.invoke(app, args)
        assert result.exit_code == 1
        assert (
            "already" in result.output.lower()
            or "already" in (result.stderr or "").lower()
        )

    def test_digest_for_new_object(self, runner, storage_root, tmp_path):
        """--digest sets algorithm for new objects."""
        staging = tmp_path / "staging"
        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "stage",
                "--staging-root",
                str(staging),
                "new",
                "obj:sha256",
                "--digest",
                "sha256",
            ],
        )
        assert result.exit_code == 0, result.output

        from ocfl_cli.staging import StagingStore

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        inv = store.load_inventory(root_path, "obj:sha256")
        assert inv.digest_algorithm == "sha256"

    def test_digest_inheritance_existing(self, runner, storage_root, tmp_path):
        """Existing objects inherit digest algorithm from head inventory."""
        staging = tmp_path / "staging"
        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "stage",
                "--staging-root",
                str(staging),
                "new",
                "obj:test",
            ],
        )
        assert result.exit_code == 0, result.output

        from ocfl_cli.staging import StagingStore

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        inv = store.load_inventory(root_path, "obj:test")
        # storage_root fixture uses SHA512
        assert inv.digest_algorithm == "sha512"

    def test_digest_ignored_for_existing(self, runner, storage_root, tmp_path):
        """--digest is ignored for existing objects (they inherit from head)."""
        staging = tmp_path / "staging"
        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "stage",
                "--staging-root",
                str(staging),
                "new",
                "obj:test",
                "--digest",
                "sha256",
            ],
        )
        assert result.exit_code == 0, result.output

        from ocfl_cli.staging import StagingStore

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        inv = store.load_inventory(root_path, "obj:test")
        # Should still be sha512 from existing object, not sha256
        assert inv.digest_algorithm == "sha512"
