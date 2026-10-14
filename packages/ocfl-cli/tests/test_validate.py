import hashlib
import io
import json
from pathlib import Path
from typing import cast

from runner_helpers import ObjRunner
from seed_root import SEED_ID

from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app


class TestValidateRoot:
    def test_valid_root(self, runner: ObjRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "validate"])
        assert result.exit_code == ExitCode.SUCCESS
        assert "Valid" in result.stdout

    def test_valid_root_json(self, runner: ObjRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "--json", "validate"])
        assert result.exit_code == ExitCode.SUCCESS
        data = json.loads(result.stdout)
        assert data["valid"] is True

    def test_invalid_root(self, runner: ObjRunner, tmp_path: Path):
        result = runner.invoke(app, ["--root", str(tmp_path), "validate"])
        assert result.exit_code == ExitCode.GENERIC

    def test_nonexistent_root(self, runner: ObjRunner, tmp_path: Path):
        bad = tmp_path / "nope"
        result = runner.invoke(app, ["--root", str(bad), "validate"])
        assert result.exit_code == ExitCode.GENERIC

    def test_no_check_digests(self, runner: ObjRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "validate", "--no-check-digests"]
        )
        assert result.exit_code == ExitCode.SUCCESS

    def test_root_emits_phase_headers_by_default(
        self, runner: ObjRunner, storage_root: Path
    ):
        result = runner.invoke(app, ["--root", str(storage_root), "validate"])
        assert result.exit_code == ExitCode.SUCCESS
        assert "Valid" in result.stdout
        assert "Validating OCFL storage root" in result.stderr
        assert "Checking root structure" in result.stderr
        assert "Validating objects (content digests enabled)" in result.stderr

    def test_root_lists_each_object_ok_by_default(
        self, runner: ObjRunner, storage_root: Path
    ):
        result = runner.invoke(app, ["--root", str(storage_root), "validate"])
        assert result.exit_code == ExitCode.SUCCESS
        assert "  obj:test: ok" in result.stderr

    def test_root_lists_invalid_object_with_error_count_by_default(
        self, runner: ObjRunner, storage_root: Path
    ):
        from ocfl_lib import LocalFS, StorageRoot

        root = StorageRoot.open(LocalFS(storage_root))
        obj_path = storage_root / root.object_path(SEED_ID)
        inv_path = obj_path / "inventory.json"
        inv = json.loads(inv_path.read_text())
        inv["digestAlgorithm"] = "md5"
        inv_path.write_text(json.dumps(inv, separators=(",", ":")))

        result = runner.invoke(app, ["--root", str(storage_root), "validate"])
        assert result.exit_code == ExitCode.GENERIC
        assert "  obj:test: 6 errors" in result.stderr

    def test_root_json_reports_invalid_contained_object(
        self, runner: ObjRunner, storage_root: Path
    ):
        from ocfl_lib import LocalFS, StorageRoot

        root = StorageRoot.open(LocalFS(storage_root))
        obj_path = storage_root / root.object_path(SEED_ID)
        inv_path = obj_path / "inventory.json"
        inv = json.loads(inv_path.read_text())
        inv["digestAlgorithm"] = "md5"
        inv_path.write_text(json.dumps(inv, separators=(",", ":")))

        result = runner.invoke(app, ["--root", str(storage_root), "--json", "validate"])
        assert result.exit_code == ExitCode.GENERIC
        data = json.loads(result.stdout)
        assert data["valid"] is False
        assert any(e["code"] == "E025a" for e in data["errors"])

    def test_root_json_reports_malformed_contained_inventory(
        self, runner: ObjRunner, storage_root: Path
    ):
        from ocfl_lib import LocalFS, StorageRoot

        root = StorageRoot.open(LocalFS(storage_root))
        obj_path = storage_root / root.object_path(SEED_ID)
        (obj_path / "inventory.json").write_text("{")

        result = runner.invoke(app, ["--root", str(storage_root), "--json", "validate"])

        assert result.exit_code == ExitCode.GENERIC
        data = json.loads(result.stdout)
        assert data["valid"] is False
        assert any(e["code"] == "E033" for e in data["errors"])

    def test_root_marks_unvalidated_objects_when_truncated_by_default(
        self, storage_root: Path
    ):
        from rich.console import Console

        from ocfl_cli.archive import validate as validate_module
        from ocfl_lib import LocalFS, StorageRoot, ValidationReport

        fs = LocalFS(storage_root)
        obj_path = StorageRoot.open(fs).object_path(SEED_ID)
        report = ValidationReport(objects_checked=1, truncated=True)
        out = Console(file=io.StringIO(), color_system=None)

        validate_module.emit_root_object_results(
            out, True, fs, {obj_path: SEED_ID}, report
        )

        text = cast("io.StringIO", out.file).getvalue()
        assert "  obj:test: not validated (max errors reached)" in text
        assert "  obj:test: ok" not in text


class TestValidateObject:
    def test_valid_object(self, runner: ObjRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "validate", SEED_ID])
        assert result.exit_code == ExitCode.SUCCESS
        assert "Valid" in result.stdout

    def test_valid_object_json(self, runner: ObjRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "--json", "validate", SEED_ID]
        )
        assert result.exit_code == ExitCode.SUCCESS
        data = json.loads(result.stdout)
        assert data["valid"] is True
        assert data["errors"] == []

    def test_invalid_object_id(self, runner: ObjRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "validate", "nonexistent"]
        )
        assert result.exit_code == ExitCode.GENERIC

    def test_no_check_digests(self, runner: ObjRunner, storage_root: Path):
        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "validate",
                SEED_ID,
                "--no-check-digests",
            ],
        )
        assert result.exit_code == ExitCode.SUCCESS

    def test_object_emits_phase_headers_by_default(
        self, runner: ObjRunner, storage_root: Path
    ):
        result = runner.invoke(app, ["--root", str(storage_root), "validate", SEED_ID])
        assert result.exit_code == ExitCode.SUCCESS
        assert "Validating object: obj:test" in result.stderr
        assert "Checking OCFL structure" in result.stderr
        assert "Verifying content digests" in result.stderr
        assert "Verifying OTS attestations" not in result.stderr

    def test_skips_digests_line_with_no_check_digests_by_default(
        self, runner: ObjRunner, storage_root: Path
    ):
        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "validate",
                SEED_ID,
                "--no-check-digests",
            ],
        )
        assert result.exit_code == ExitCode.SUCCESS
        assert "Verifying content digests" not in result.stderr

    def test_progress_silenced_by_json(self, runner: ObjRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "--json", "validate"])
        assert result.exit_code == ExitCode.SUCCESS
        assert "Validating OCFL storage root" not in result.stderr
        data = json.loads(result.stdout)
        assert data["valid"] is True

    def test_progress_silenced_by_quiet(self, runner: ObjRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "--quiet", "validate"]
        )
        assert result.exit_code == ExitCode.SUCCESS
        lines = result.stderr.splitlines()
        assert all(line.startswith("Warning: [W") for line in lines), lines


def _inflate_head_message(storage_root: Path) -> None:
    """Rewrite obj:test head inventory with an over-limit (L002) message."""
    obj = storage_root / SEED_ID
    inv = json.loads((obj / "inventory.json").read_bytes())
    inv["versions"][inv["head"]]["message"] = "x" * (64 * 1024 + 1)
    raw = json.dumps(inv, indent=2).encode()
    digest = hashlib.sha512(raw).hexdigest()
    for d in (obj, obj / inv["head"]):
        (d / "inventory.json").write_bytes(raw)
        (d / "inventory.json.sha512").write_text(f"{digest} inventory.json\n")


class TestValidateLimitInfo:
    """L-codes are info: reported, never fail validation."""

    def test_object_json_reports_info_and_valid(
        self, runner: ObjRunner, storage_root: Path
    ):
        _inflate_head_message(storage_root)
        result = runner.invoke(
            app, ["--root", str(storage_root), "--json", "validate", SEED_ID]
        )
        assert result.exit_code == ExitCode.SUCCESS, result.output
        data = json.loads(result.stdout)
        assert data["valid"] is True
        assert data["errors"] == []
        assert {i["code"] for i in data["info"]} == {"L002"}

    def test_root_json_merges_object_info(self, runner: ObjRunner, storage_root: Path):
        _inflate_head_message(storage_root)
        result = runner.invoke(app, ["--root", str(storage_root), "--json", "validate"])
        assert result.exit_code == ExitCode.SUCCESS, result.output
        data = json.loads(result.stdout)
        assert data["valid"] is True
        assert {i["code"] for i in data["info"]} == {"L002"}
        assert all(i["message"].startswith("obj:test: ") for i in data["info"])

    def test_object_text_shows_info(self, runner: ObjRunner, storage_root: Path):
        _inflate_head_message(storage_root)
        result = runner.invoke(app, ["--root", str(storage_root), "validate", SEED_ID])
        assert result.exit_code == ExitCode.SUCCESS, result.output
        assert "Info: [L002]" in result.stderr
        assert "Valid" in result.stdout
