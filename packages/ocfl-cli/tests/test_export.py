import json
from pathlib import Path

import pytest
from seed_root import SEED_ID
from typer.testing import CliRunner

from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app
from ocfl_lib import DigestAlgorithm, bytes_digest


def _rewrite_root_logical_paths(storage_root: Path, logical_paths: list[str]) -> None:
    inv_path = _find_object_root_inventory(storage_root)
    inv = json.loads(inv_path.read_bytes())
    head_state = inv["versions"][inv["head"]]["state"]
    digest = next(iter(head_state))
    inv["versions"][inv["head"]]["state"] = {digest: logical_paths}

    raw = json.dumps(
        inv,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    inv_path.write_bytes(raw)

    alg = DigestAlgorithm.from_ocfl_name(inv["digestAlgorithm"])
    digest_text = bytes_digest(raw, alg)
    inv_path.with_name(f"inventory.json.{alg.ocfl_name}").write_text(
        f"{digest_text} inventory.json\n"
    )


def _find_object_root_inventory(storage_root: Path) -> Path:
    for inv_path in storage_root.rglob("inventory.json"):
        inv = json.loads(inv_path.read_bytes())
        if inv.get("id") != SEED_ID:
            continue
        if any(
            child.name.startswith("0=ocfl_object_")
            for child in inv_path.parent.iterdir()
        ):
            return inv_path
    raise AssertionError("object root inventory not found")


class TestExport:
    def test_export_full_version(
        self, runner: CliRunner, storage_root: Path, tmp_path: Path
    ):
        out = tmp_path / "out"
        out.mkdir()
        result = runner.invoke(
            app,
            ["--root", str(storage_root), "export", SEED_ID, "--to", str(out)],
        )
        assert result.exit_code == ExitCode.SUCCESS
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
                SEED_ID,
                "--object-version",
                "v1",
                "--to",
                str(out),
            ],
        )
        assert result.exit_code == ExitCode.SUCCESS
        assert (out / "file1.txt").read_text() == "hello"
        assert not (out / "file2.txt").exists()

    def test_nonexistent_version_reports_clean_error(
        self, runner: CliRunner, storage_root: Path, tmp_path: Path
    ):
        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "export",
                SEED_ID,
                "--object-version",
                "v9",
                "--to",
                str(tmp_path / "out"),
            ],
        )
        assert result.exit_code == ExitCode.GENERIC
        assert "not found" in result.stderr.lower()
        assert result.exception.__class__.__name__ == "SystemExit"

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
                SEED_ID,
                "--file",
                "file1.txt",
                "--to",
                str(out),
            ],
        )
        assert result.exit_code == ExitCode.SUCCESS
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
                SEED_ID,
                "--file",
                "file2*",
                "--to",
                str(out),
            ],
        )
        assert result.exit_code == ExitCode.SUCCESS
        assert not (out / "file1.txt").exists()
        assert (out / "file2.txt").read_text() == "world"

    def test_export_to_stdout(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "export",
                SEED_ID,
                "--file",
                "file1.txt",
                "--to",
                "-",
            ],
        )
        assert result.exit_code == ExitCode.SUCCESS
        assert result.stdout == "hello"

    def test_export_to_stdout_refuses_more_than_one_match(
        self, runner: CliRunner, storage_root: Path
    ):
        result = runner.invoke(
            app,
            ["--root", str(storage_root), "export", SEED_ID, "--to", "-"],
        )
        assert result.exit_code == ExitCode.USAGE, result.output
        assert "--to - writes one file" in result.output
        assert result.stdout == ""

    def test_export_replace(
        self, runner: CliRunner, storage_root: Path, tmp_path: Path
    ):
        out = tmp_path / "out"
        out.mkdir()
        (out / "file1.txt").write_text("old")
        result = runner.invoke(
            app,
            ["--root", str(storage_root), "export", SEED_ID, "--to", str(out)],
        )
        assert result.exit_code == ExitCode.GENERIC

        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "export",
                SEED_ID,
                "--to",
                str(out),
                "--replace",
            ],
        )
        assert result.exit_code == ExitCode.SUCCESS
        assert (out / "file1.txt").read_text() == "hello"

    def test_export_rejects_parent_escape_without_writing_outside(
        self, runner: CliRunner, storage_root: Path, tmp_path: Path
    ):
        _rewrite_root_logical_paths(storage_root, ["../escape.txt"])
        out = tmp_path / "out"
        outside = tmp_path / "escape.txt"

        result = runner.invoke(
            app,
            ["--root", str(storage_root), "export", SEED_ID, "--to", str(out)],
        )

        assert result.exit_code == ExitCode.GENERIC
        assert "invalid logical path" in result.stderr
        assert not outside.exists()

    def test_export_rejects_absolute_logical_path(
        self, runner: CliRunner, storage_root: Path, tmp_path: Path
    ):
        _rewrite_root_logical_paths(storage_root, [str(tmp_path / "escape.txt")])

        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "export",
                SEED_ID,
                "--to",
                str(tmp_path / "out"),
            ],
        )

        assert result.exit_code == ExitCode.GENERIC
        assert "invalid logical path" in result.stderr

    @pytest.mark.parametrize(
        "logical_path",
        [
            "a/../b.txt",
            "a//b.txt",
            "",
        ],
    )
    def test_export_rejects_invalid_logical_path_segments(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        logical_path: str,
    ):
        _rewrite_root_logical_paths(storage_root, [logical_path])

        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "export",
                SEED_ID,
                "--to",
                str(tmp_path / "out"),
            ],
        )

        assert result.exit_code == ExitCode.GENERIC
        assert "invalid logical path" in result.stderr

    def test_export_allows_ordinary_nested_logical_path(
        self, runner: CliRunner, storage_root: Path, tmp_path: Path
    ):
        _rewrite_root_logical_paths(storage_root, ["nested/file.txt"])
        out = tmp_path / "out"

        result = runner.invoke(
            app,
            ["--root", str(storage_root), "export", SEED_ID, "--to", str(out)],
        )

        assert result.exit_code == ExitCode.SUCCESS
        assert (out / "nested" / "file.txt").read_text() == "world"

    def test_export_replace_does_not_bypass_logical_path_containment(
        self, runner: CliRunner, storage_root: Path, tmp_path: Path
    ):
        _rewrite_root_logical_paths(storage_root, ["../escape.txt"])
        out = tmp_path / "out"
        outside = tmp_path / "escape.txt"

        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "export",
                SEED_ID,
                "--to",
                str(out),
                "--replace",
            ],
        )

        assert result.exit_code == ExitCode.GENERIC
        assert "invalid logical path" in result.stderr
        assert not outside.exists()

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
        assert result.exit_code == ExitCode.GENERIC
