import shutil

import boto3
import pytest
from moto import mock_aws
from typer.testing import CliRunner

from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app
from ocfl_lib import (
    DigestAlgorithm,
    LocalFS,
    Spec,
    StorageRoot,
    get_layout,
    scan_dir,
)
from ocfl_lib.fs.s3 import S3FS

BUCKET = "test-bucket"


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def s3_env():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture
def s3_root_url():
    return f"s3://{BUCKET}/ocfl-root"


@pytest.fixture
def s3_storage_root(s3_env, s3_root_url, tmp_path):
    fs = S3FS(BUCKET, "ocfl-root", client=s3_env)
    layout = get_layout("0002-flat-direct-storage-layout")
    root = StorageRoot.init(fs, fs, spec=Spec.V1_1, layout=layout)

    local_fs = LocalFS(tmp_path)
    obj_dir = tmp_path / "test-files"
    obj_dir.mkdir()
    (obj_dir / "file1.txt").write_text("hello")
    stage = scan_dir(local_fs, "test-files", DigestAlgorithm.SHA512)
    root.commit(fs, "obj:test", stage, message="v1")

    (obj_dir / "file2.txt").write_text("world")
    stage2 = scan_dir(local_fs, "test-files", DigestAlgorithm.SHA512)
    root.commit(fs, "obj:test", stage2, message="v2")

    shutil.rmtree(obj_dir)
    return s3_root_url


class TestInit:
    def test_init_s3(self, runner: CliRunner, s3_env, s3_root_url):
        result = runner.invoke(
            app,
            [
                "--root",
                s3_root_url,
                "init",
                "--layout",
                "0002-flat-direct-storage-layout",
            ],
        )
        assert result.exit_code == ExitCode.SUCCESS, result.output
        resp = s3_env.list_objects_v2(Bucket=BUCKET, Prefix="ocfl-root/0=ocfl_1.1")
        assert resp.get("KeyCount", 0) > 0


class TestRoundTrip:
    def test_ls(self, runner: CliRunner, s3_storage_root):
        result = runner.invoke(app, ["--root", s3_storage_root, "ls"])
        assert result.exit_code == ExitCode.SUCCESS, result.output
        assert "obj:test" in result.stdout

    def test_log(self, runner: CliRunner, s3_storage_root):
        result = runner.invoke(app, ["--root", s3_storage_root, "log", "obj:test"])
        assert result.exit_code == ExitCode.SUCCESS, result.output
        assert "v1" in result.stdout
        assert "v2" in result.stdout

    def test_show(self, runner: CliRunner, s3_storage_root):
        result = runner.invoke(app, ["--root", s3_storage_root, "show", "obj:test"])
        assert result.exit_code == ExitCode.SUCCESS, result.output
        assert "v2" in result.stdout

    def test_info(self, runner: CliRunner, s3_storage_root):
        result = runner.invoke(app, ["--root", s3_storage_root, "info"])
        assert result.exit_code == ExitCode.SUCCESS, result.output
        assert "1.1" in result.stdout

    def test_validate_root(self, runner: CliRunner, s3_storage_root):
        result = runner.invoke(app, ["--root", s3_storage_root, "validate"])
        assert result.exit_code == ExitCode.SUCCESS, result.output
        assert "Valid" in result.stdout

    def test_validate_object(self, runner: CliRunner, s3_storage_root):
        result = runner.invoke(app, ["--root", s3_storage_root, "validate", "obj:test"])
        assert result.exit_code == ExitCode.SUCCESS, result.output
        assert "Valid" in result.stdout

    def test_diff(self, runner: CliRunner, s3_storage_root):
        result = runner.invoke(
            app, ["--root", s3_storage_root, "diff", "obj:test", "v1", "v2"]
        )
        assert result.exit_code == ExitCode.SUCCESS, result.output
        assert "file2.txt" in result.stdout

    def test_export(self, runner: CliRunner, s3_storage_root, tmp_path):
        out = tmp_path / "export-out"
        out.mkdir()
        result = runner.invoke(
            app,
            ["--root", s3_storage_root, "export", "obj:test", "--to", str(out)],
        )
        assert result.exit_code == ExitCode.SUCCESS, result.output
        assert (out / "file1.txt").read_text() == "hello"
        assert (out / "file2.txt").read_text() == "world"

    def test_purge(self, runner: CliRunner, s3_storage_root):
        result = runner.invoke(
            app,
            ["--root", s3_storage_root, "purge", "obj:test", "--yes"],
        )
        assert result.exit_code == ExitCode.SUCCESS, result.output
        ls_result = runner.invoke(app, ["--root", s3_storage_root, "ls"])
        assert ls_result.exit_code == ExitCode.SUCCESS
        assert "obj:test" not in ls_result.stdout
