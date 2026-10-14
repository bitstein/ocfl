import json
from pathlib import Path

from runner_helpers import ObjRunner

from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app
from ocfl_lib import LocalFS, StorageRoot


class TestInit:
    def test_init_creates_valid_root(self, runner: ObjRunner, tmp_path: Path):
        result = runner.invoke(app, ["--root", str(tmp_path), "init"])
        assert result.exit_code == ExitCode.SUCCESS
        assert (tmp_path / "0=ocfl_1.1").exists()

    def test_init_default_layout(self, runner: ObjRunner, tmp_path: Path):
        runner.invoke(app, ["--root", str(tmp_path), "init"])
        data = json.loads((tmp_path / "ocfl_layout.json").read_text())
        assert data["extension"] == "0004-hashed-n-tuple-storage-layout"

    def test_init_reopen(self, runner: ObjRunner, tmp_path: Path):
        runner.invoke(app, ["--root", str(tmp_path), "init"])
        fs = LocalFS(tmp_path)
        root = StorageRoot.open(fs)
        assert root.layout is not None
        assert root.layout.name == "0004-hashed-n-tuple-storage-layout"

    def test_init_custom_layout(self, runner: ObjRunner, tmp_path: Path):
        result = runner.invoke(
            app,
            [
                "--root",
                str(tmp_path),
                "init",
                "--layout",
                "0002-flat-direct-storage-layout",
            ],
        )
        assert result.exit_code == ExitCode.SUCCESS
        data = json.loads((tmp_path / "ocfl_layout.json").read_text())
        assert data["extension"] == "0002-flat-direct-storage-layout"

    def test_init_existing_root_refuses_to_rewrite_layout(
        self, runner: ObjRunner, tmp_path: Path
    ):
        first = runner.invoke(app, ["--root", str(tmp_path), "init"])
        assert first.exit_code == ExitCode.SUCCESS

        result = runner.invoke(
            app,
            [
                "--root",
                str(tmp_path),
                "init",
                "--layout",
                "0002-flat-direct-storage-layout",
            ],
        )
        assert result.exit_code == ExitCode.PHASE
        assert "already initialized" in result.output

        data = json.loads((tmp_path / "ocfl_layout.json").read_text())
        assert data["extension"] == "0004-hashed-n-tuple-storage-layout"

    def test_init_layout_config(self, runner: ObjRunner, tmp_path: Path):
        cfg = json.dumps(
            {
                "digestAlgorithm": "sha256",
                "tupleSize": 2,
                "numberOfTuples": 2,
                "shortObjectRoot": True,
            }
        )
        result = runner.invoke(
            app,
            ["--root", str(tmp_path), "init", "--layout-config", cfg],
        )
        assert result.exit_code == ExitCode.SUCCESS
        ext_config = json.loads(
            (
                tmp_path
                / "extensions"
                / "0004-hashed-n-tuple-storage-layout"
                / "config.json"
            ).read_text()
        )
        assert ext_config["tupleSize"] == 2

    def test_init_layout_description(self, runner: ObjRunner, tmp_path: Path):
        result = runner.invoke(
            app,
            ["--root", str(tmp_path), "init", "--layout-description", "my layout"],
        )
        assert result.exit_code == ExitCode.SUCCESS
        data = json.loads((tmp_path / "ocfl_layout.json").read_text())
        assert data["description"] == "my layout"

    def test_init_spec_1_0(self, runner: ObjRunner, tmp_path: Path):
        result = runner.invoke(app, ["--root", str(tmp_path), "init", "--spec", "1.0"])
        assert result.exit_code == ExitCode.SUCCESS
        assert (tmp_path / "0=ocfl_1.0").exists()

    def test_init_invalid_spec(self, runner: ObjRunner, tmp_path: Path):
        result = runner.invoke(app, ["--root", str(tmp_path), "init", "--spec", "2.0"])
        assert result.exit_code == ExitCode.USAGE
        assert "invalid value" in result.output.lower()

    def test_init_invalid_layout(self, runner: ObjRunner, tmp_path: Path):
        result = runner.invoke(
            app,
            ["--root", str(tmp_path), "init", "--layout", "nonexistent-layout"],
        )
        assert result.exit_code == ExitCode.GENERIC
