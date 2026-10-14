import pytest
from typer.testing import CliRunner

from ocfl_cli.main import app


@pytest.fixture
def runner():
    return CliRunner()


class TestStageNew:
    def test_new_fresh_object(self, runner, storage_root, tmp_path):
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

        from ocfl_cli.store.staging import StagingStore

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        assert store.exists(root_path, "obj:new")
        inv = store.load_inventory(root_path, "obj:new")
        assert inv.id == "obj:new"
        assert inv.head == "v1"
        assert inv.manifest == {}
        assert inv.versions["v1"].state == {}

    def test_new_existing_object(self, runner, storage_root, tmp_path):
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

        from ocfl_cli.store.staging import StagingStore

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        inv = store.load_inventory(root_path, "obj:test")
        # Existing object has v2 head, so staged should be v3
        assert inv.head == "v3"
        assert len(inv.manifest) > 0
        assert inv.versions["v3"].state == {}

    def test_error_on_double_new(self, runner, storage_root, tmp_path):
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
        result = runner.invoke(app, args)
        assert result.exit_code == 1
        assert (
            "already" in result.output.lower()
            or "already" in (result.stderr or "").lower()
        )

    def test_digest_for_new_object(self, runner, storage_root, tmp_path):
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

        from ocfl_cli.store.staging import StagingStore

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        inv = store.load_inventory(root_path, "obj:sha256")
        assert inv.digest_algorithm == "sha256"

    def test_invalid_digest_is_usage_error(self, runner, storage_root, tmp_path):
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
                "obj:bad-digest",
                "--digest",
                "bogus",
            ],
        )
        assert result.exit_code == 2
        assert "invalid value" in result.output.lower()

        from ocfl_cli.store.staging import StagingStore

        store = StagingStore(staging)
        assert not store.exists(str(storage_root.resolve()), "obj:bad-digest")

    @pytest.mark.parametrize("digest", ["md5", "sha1", "blake2b-512"])
    def test_non_spec_content_digest_is_usage_error(
        self, runner, storage_root, tmp_path, digest
    ):
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
                "obj:bad-content-digest",
                "--digest",
                digest,
            ],
        )
        assert result.exit_code == 2
        assert "sha512" in result.output
        assert "sha256" in result.output

        from ocfl_cli.store.staging import StagingStore

        store = StagingStore(staging)
        assert not store.exists(str(storage_root.resolve()), "obj:bad-content-digest")

    def test_digest_inheritance_existing(self, runner, storage_root, tmp_path):
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

        from ocfl_cli.store.staging import StagingStore

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        inv = store.load_inventory(root_path, "obj:test")
        # storage_root fixture uses SHA512
        assert inv.digest_algorithm == "sha512"

    def test_digest_ignored_for_existing(self, runner, storage_root, tmp_path):
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

        from ocfl_cli.store.staging import StagingStore

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        inv = store.load_inventory(root_path, "obj:test")
        assert inv.digest_algorithm == "sha512"
