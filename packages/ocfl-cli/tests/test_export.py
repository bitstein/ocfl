"""Tests for ocfl export command."""

from pathlib import Path

from typer.testing import CliRunner

from ocfl_cli.main import app


class TestExport:
    def test_export_full_version(
        self, runner: CliRunner, storage_root: Path, tmp_path: Path
    ):
        out = tmp_path / "out"
        out.mkdir()
        result = runner.invoke(
            app,
            ["--root", str(storage_root), "export", "obj:test", "--to", str(out)],
        )
        assert result.exit_code == 0
        assert (out / "file1.txt").read_text() == "hello"
        assert (out / "file2.txt").read_text() == "world"

    def test_export_v1(self, runner: CliRunner, storage_root: Path, tmp_path: Path):
        out = tmp_path / "out"
        out.mkdir()
        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "export",
                "obj:test",
                "--version",
                "v1",
                "--to",
                str(out),
            ],
        )
        assert result.exit_code == 0
        assert (out / "file1.txt").read_text() == "hello"
        assert not (out / "file2.txt").exists()

    def test_export_single_file(
        self, runner: CliRunner, storage_root: Path, tmp_path: Path
    ):
        out = tmp_path / "out"
        out.mkdir()
        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "export",
                "obj:test",
                "--file",
                "file1.txt",
                "--to",
                str(out),
            ],
        )
        assert result.exit_code == 0
        assert (out / "file1.txt").read_text() == "hello"
        assert not (out / "file2.txt").exists()

    def test_export_glob_pattern(
        self, runner: CliRunner, storage_root: Path, tmp_path: Path
    ):
        out = tmp_path / "out"
        out.mkdir()
        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "export",
                "obj:test",
                "--file",
                "file2*",
                "--to",
                str(out),
            ],
        )
        assert result.exit_code == 0
        assert not (out / "file1.txt").exists()
        assert (out / "file2.txt").read_text() == "world"

    def test_export_to_stdout(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "export",
                "obj:test",
                "--file",
                "file1.txt",
                "--to",
                "-",
            ],
        )
        assert result.exit_code == 0
        assert result.stdout == "hello"

    def test_export_replace(
        self, runner: CliRunner, storage_root: Path, tmp_path: Path
    ):
        out = tmp_path / "out"
        out.mkdir()
        (out / "file1.txt").write_text("old")
        # Without --replace should fail
        result = runner.invoke(
            app,
            ["--root", str(storage_root), "export", "obj:test", "--to", str(out)],
        )
        assert result.exit_code == 1

        # With --replace should succeed
        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "export",
                "obj:test",
                "--to",
                str(out),
                "--replace",
            ],
        )
        assert result.exit_code == 0
        assert (out / "file1.txt").read_text() == "hello"

    def test_export_nonexistent_object(
        self, runner: CliRunner, storage_root: Path, tmp_path: Path
    ):
        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "export",
                "nonexistent",
                "--to",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 1
