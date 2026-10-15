import json
from hashlib import sha512

import pytest
from seed_root import SEED_ID
from typer.testing import CliRunner

from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app


@pytest.fixture
def runner():
    return CliRunner()


def _invoke(runner, storage_root, staging, *args):
    return runner.invoke(
        app,
        ["--root", str(storage_root), "stage", "--staging-root", str(staging), *args],
    )


def _setup_staged_new(runner, storage_root, tmp_path):
    staging = tmp_path / "staging"
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "a.txt").write_text("aaa")
    sub = src_dir / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("bbb")

    _invoke(runner, storage_root, staging, "new", "obj:new")
    _invoke(runner, storage_root, staging, "add", "obj:new", str(src_dir))
    return staging


class TestStageLs:
    def test_ls_lists_files(self, runner, storage_root, tmp_path):
        staging = _setup_staged_new(runner, storage_root, tmp_path)
        result = _invoke(runner, storage_root, staging, "ls", "obj:new")
        assert result.exit_code == ExitCode.SUCCESS, result.output
        assert "a.txt" in result.output
        assert "sub/b.txt" in result.output

    def test_ls_with_digests(self, runner, storage_root, tmp_path):
        staging = _setup_staged_new(runner, storage_root, tmp_path)
        result = _invoke(runner, storage_root, staging, "ls", "obj:new", "--digests")
        assert result.exit_code == ExitCode.SUCCESS, result.output
        assert result.output == (
            f"{sha512(b'aaa').hexdigest()}  a.txt\n"
            f"{sha512(b'bbb').hexdigest()}  sub/b.txt\n"
        )

    def test_ls_json(self, runner, storage_root, tmp_path):
        staging = _setup_staged_new(runner, storage_root, tmp_path)
        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "--json",
                "stage",
                "--staging-root",
                str(staging),
                "ls",
                "obj:new",
            ],
        )
        assert result.exit_code == ExitCode.SUCCESS, result.output
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert {"path": "a.txt"} in data

    def test_ls_without_object_lists_staging_areas(
        self, runner, storage_root, tmp_path
    ):
        staging = _setup_staged_new(runner, storage_root, tmp_path)
        args = ["--root", str(storage_root), "--json", "stage"]
        result = runner.invoke(app, [*args, "--staging-root", str(staging), "ls"])
        assert result.exit_code == ExitCode.SUCCESS, result.output
        assert json.loads(result.output) == [{"object_id": "obj:new", "phase": None}]
        other = tmp_path / "other-staging"
        result = runner.invoke(app, [*args, "--staging-root", str(other), "ls"])
        assert json.loads(result.output) == []

    def test_ls_no_staging_errors(self, runner, storage_root, tmp_path):
        staging = tmp_path / "staging"
        result = _invoke(runner, storage_root, staging, "ls", "obj:nope")
        assert result.exit_code == ExitCode.GENERIC


class TestStageStatus:
    def test_status_new_object(self, runner, storage_root, tmp_path):
        staging = _setup_staged_new(runner, storage_root, tmp_path)
        result = _invoke(runner, storage_root, staging, "status", "obj:new")
        assert result.exit_code == ExitCode.SUCCESS, result.output
        assert "added" in result.output.lower() or "A " in result.output
        assert "a.txt" in result.output
        assert "sub/b.txt" in result.output

    def test_status_existing_object_with_changes(self, runner, storage_root, tmp_path):
        staging = tmp_path / "staging"
        _invoke(runner, storage_root, staging, "new", SEED_ID)

        new_file = tmp_path / "new.txt"
        new_file.write_text("new content")
        _invoke(runner, storage_root, staging, "add", SEED_ID, str(new_file))

        result = _invoke(runner, storage_root, staging, "status", SEED_ID)
        assert result.exit_code == ExitCode.SUCCESS, result.output
        assert "new.txt" in result.output

    def test_status_json(self, runner, storage_root, tmp_path):
        staging = _setup_staged_new(runner, storage_root, tmp_path)
        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "--json",
                "stage",
                "--staging-root",
                str(staging),
                "status",
                "obj:new",
            ],
        )
        assert result.exit_code == ExitCode.SUCCESS, result.output
        data = json.loads(result.output)
        assert isinstance(data, dict)
        assert data["extension"] is None
        assert isinstance(data["changes"], list)
        assert data["metadata"] == {
            "message": None,
            "user.name": None,
            "user.address": None,
        }
        for entry in data["changes"]:
            assert "path" in entry
            assert "action" in entry

    def test_status_shows_staged_metadata_state(self, runner, storage_root, tmp_path):
        staging = tmp_path / "staging"
        result = _invoke(
            runner,
            storage_root,
            staging,
            "new",
            "obj:metadata",
            "-m",
            "Initial deposit",
            "--name",
            "Alice",
        )
        assert result.exit_code == ExitCode.SUCCESS, result.output

        result = _invoke(runner, storage_root, staging, "status", "obj:metadata")

        assert result.exit_code == ExitCode.SUCCESS, result.output
        assert "Metadata:" in result.output
        assert "message: Initial deposit" in result.output
        assert "user.name: Alice" in result.output
        assert "user.address: missing" in result.output

    def test_status_json_shows_partial_staged_metadata_state(
        self, runner, storage_root, tmp_path
    ):
        staging = tmp_path / "staging"
        result = _invoke(
            runner,
            storage_root,
            staging,
            "new",
            "obj:metadata-json",
            "-m",
            "Initial deposit",
            "--name",
            "Alice",
        )
        assert result.exit_code == ExitCode.SUCCESS, result.output

        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "--json",
                "stage",
                "--staging-root",
                str(staging),
                "status",
                "obj:metadata-json",
            ],
        )

        assert result.exit_code == ExitCode.SUCCESS, result.output
        data = json.loads(result.output)
        assert data["metadata"] == {
            "message": "Initial deposit",
            "user.name": "Alice",
            "user.address": None,
        }

    def test_status_no_staging_errors(self, runner, storage_root, tmp_path):
        staging = tmp_path / "staging"
        result = _invoke(runner, storage_root, staging, "status", "obj:nope")
        assert result.exit_code == ExitCode.GENERIC
