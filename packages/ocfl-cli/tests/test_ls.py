import json
from hashlib import sha512
from pathlib import Path

from runner_helpers import ObjRunner
from seed_root import SEED_ID

from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app


class TestLsRoot:
    def test_list_objects(self, runner: ObjRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "ls"])
        assert result.exit_code == ExitCode.SUCCESS
        assert SEED_ID in result.stdout

    def test_list_objects_json(self, runner: ObjRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "--json", "ls"])
        assert result.exit_code == ExitCode.SUCCESS
        data = json.loads(result.stdout)
        assert SEED_ID in data

    def test_empty_root(self, runner: ObjRunner, tmp_path: Path):
        from ocfl_lib import LocalFS, Spec, StorageRoot, get_layout

        fs = LocalFS(tmp_path)
        StorageRoot.init(
            fs, fs, spec=Spec.V1_1, layout=get_layout("0002-flat-direct-storage-layout")
        )
        result = runner.invoke(app, ["--root", str(tmp_path), "ls"])
        assert result.exit_code == ExitCode.SUCCESS


class TestLsObject:
    def test_list_files_head(self, runner: ObjRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "ls", SEED_ID])
        assert result.exit_code == ExitCode.SUCCESS
        assert "file1.txt" in result.stdout
        assert "file2.txt" in result.stdout

    def test_list_files_version(self, runner: ObjRunner, storage_root: Path):
        result = runner.invoke(
            app,
            ["--root", str(storage_root), "ls", SEED_ID, "--object-version", "v1"],
        )
        assert result.exit_code == ExitCode.SUCCESS
        assert "file1.txt" in result.stdout
        assert "file2.txt" not in result.stdout

    def test_list_files_bare_version(self, runner: ObjRunner, storage_root: Path):
        result = runner.invoke(
            app,
            ["--root", str(storage_root), "ls", SEED_ID, "--object-version", "1"],
        )
        assert result.exit_code == ExitCode.SUCCESS
        assert "file1.txt" in result.stdout

    def test_nonexistent_version_reports_clean_error(
        self, runner: ObjRunner, storage_root: Path
    ):
        result = runner.invoke(
            app,
            ["--root", str(storage_root), "ls", SEED_ID, "--object-version", "v9"],
        )
        assert result.exit_code == ExitCode.GENERIC
        assert "not found" in result.stderr.lower()
        assert result.exception.__class__.__name__ == "SystemExit"

    def test_list_files_json(self, runner: ObjRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "--json", "ls", SEED_ID]
        )
        assert result.exit_code == ExitCode.SUCCESS
        data = json.loads(result.stdout)
        paths = [f["path"] for f in data]
        assert "file1.txt" in paths
        assert "file2.txt" in paths

    def test_list_files_digests(self, runner: ObjRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "ls", SEED_ID, "--digests"]
        )
        assert result.exit_code == ExitCode.SUCCESS
        assert result.stdout == (
            f"{sha512(b'hello').hexdigest()}  file1.txt\n"
            f"{sha512(b'world').hexdigest()}  file2.txt\n"
        )

    def test_nonexistent_object(self, runner: ObjRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "ls", "nonexistent"])
        assert result.exit_code == ExitCode.GENERIC
