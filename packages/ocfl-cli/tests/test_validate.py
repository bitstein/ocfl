"""Tests for ocfl validate command."""

import hashlib
import json
from pathlib import Path

from typer.testing import CliRunner

from ocfl_cli.main import app


class TestValidateRoot:
    def test_valid_root(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "validate"])
        assert result.exit_code == 0
        assert "Valid" in result.stdout

    def test_valid_root_json(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "--json", "validate"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["valid"] is True

    def test_invalid_root(self, runner: CliRunner, tmp_path: Path):
        result = runner.invoke(app, ["--root", str(tmp_path), "validate"])
        assert result.exit_code == 1

    def test_nonexistent_root(self, runner: CliRunner, tmp_path: Path):
        bad = tmp_path / "nope"
        result = runner.invoke(app, ["--root", str(bad), "validate"])
        assert result.exit_code == 1

    def test_no_check_digests(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "validate", "--no-check-digests"]
        )
        assert result.exit_code == 0

    def test_root_emits_phase_headers_by_default(
        self, runner: CliRunner, storage_root: Path
    ):
        result = runner.invoke(app, ["--root", str(storage_root), "validate"])
        assert result.exit_code == 0
        assert "Valid" in result.stdout
        assert "Validating OCFL storage root" in result.stderr
        assert "Checking root structure" in result.stderr
        assert "Validating objects (content digests enabled)" in result.stderr

    def test_root_lists_each_object_ok_by_default(
        self, runner: CliRunner, storage_root: Path
    ):
        result = runner.invoke(app, ["--root", str(storage_root), "validate"])
        assert result.exit_code == 0
        assert "  obj:test: ok" in result.stderr

    def test_root_lists_invalid_object_with_error_count_by_default(
        self, runner: CliRunner, storage_root: Path
    ):
        from ocfl_lib import LocalFS, StorageRoot

        root = StorageRoot.open(LocalFS(storage_root))
        obj_path = storage_root / root.object_path("obj:test")
        inv_path = obj_path / "inventory.json"
        inv = json.loads(inv_path.read_text())
        inv["digestAlgorithm"] = "md5"
        inv_path.write_text(json.dumps(inv, separators=(",", ":")))

        result = runner.invoke(app, ["--root", str(storage_root), "validate"])
        assert result.exit_code == 1
        assert "  obj:test: 6 errors" in result.stderr

    def test_root_json_reports_invalid_contained_object(
        self, runner: CliRunner, storage_root: Path
    ):
        """Root validation must fail when a discovered object is invalid."""
        from ocfl_lib import LocalFS, StorageRoot

        root = StorageRoot.open(LocalFS(storage_root))
        obj_path = storage_root / root.object_path("obj:test")
        inv_path = obj_path / "inventory.json"
        inv = json.loads(inv_path.read_text())
        inv["digestAlgorithm"] = "md5"
        inv_path.write_text(json.dumps(inv, separators=(",", ":")))

        result = runner.invoke(app, ["--root", str(storage_root), "--json", "validate"])
        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["valid"] is False
        assert any(e["code"] == "E025a" for e in data["errors"])

    def test_root_json_reports_malformed_contained_inventory(
        self, runner: CliRunner, storage_root: Path
    ):
        from ocfl_lib import LocalFS, StorageRoot

        root = StorageRoot.open(LocalFS(storage_root))
        obj_path = storage_root / root.object_path("obj:test")
        (obj_path / "inventory.json").write_text("{")

        result = runner.invoke(app, ["--root", str(storage_root), "--json", "validate"])

        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["valid"] is False
        assert any(e["code"] == "E033" for e in data["errors"])

    def test_root_marks_unvalidated_objects_when_truncated_by_default(
        self, runner: CliRunner, storage_root: Path, monkeypatch
    ):
        from ocfl_cli.archive import validate as validate_module
        from ocfl_lib.validation.root import RootValidationSummary

        def fake_validate_root(*args, **kwargs):
            return RootValidationSummary(num_objects=1, truncated=True)

        monkeypatch.setattr(validate_module, "validate_root", fake_validate_root)

        result = runner.invoke(app, ["--root", str(storage_root), "validate"])

        assert result.exit_code == 0
        assert "  obj:test: not validated (max errors reached)" in result.stderr
        assert "  obj:test: ok" not in result.stderr


class TestValidateObject:
    def test_valid_object(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "validate", "obj:test"]
        )
        assert result.exit_code == 0
        assert "Valid" in result.stdout

    def test_valid_object_json(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "--json", "validate", "obj:test"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["valid"] is True
        assert data["errors"] == []

    def test_invalid_object_id(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "validate", "nonexistent"]
        )
        assert result.exit_code == 1

    def test_no_check_digests(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "validate",
                "obj:test",
                "--no-check-digests",
            ],
        )
        assert result.exit_code == 0

    def test_object_emits_phase_headers_by_default(
        self, runner: CliRunner, storage_root: Path
    ):
        result = runner.invoke(
            app, ["--root", str(storage_root), "validate", "obj:test"]
        )
        assert result.exit_code == 0
        assert "Validating object: obj:test" in result.stderr
        assert "Checking OCFL structure" in result.stderr
        assert "Verifying content digests" in result.stderr
        assert "Verifying OTS attestations" not in result.stderr

    def test_skips_digests_line_with_no_check_digests_by_default(
        self, runner: CliRunner, storage_root: Path
    ):
        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "validate",
                "obj:test",
                "--no-check-digests",
            ],
        )
        assert result.exit_code == 0
        assert "Verifying content digests" not in result.stderr

    def test_progress_silenced_by_json(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(app, ["--root", str(storage_root), "--json", "validate"])
        assert result.exit_code == 0
        assert "Validating OCFL storage root" not in result.stderr
        data = json.loads(result.stdout)
        assert data["valid"] is True

    def test_progress_silenced_by_quiet(self, runner: CliRunner, storage_root: Path):
        result = runner.invoke(
            app, ["--root", str(storage_root), "--quiet", "validate"]
        )
        assert result.exit_code == 0
        assert result.stderr == ""


def _inflate_head_message(storage_root: Path) -> None:
    """Rewrite obj:test head inventory with an over-limit (L002) message."""
    obj = storage_root / "obj:test"
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
        self, runner: CliRunner, storage_root: Path
    ):
        _inflate_head_message(storage_root)
        result = runner.invoke(
            app, ["--root", str(storage_root), "--json", "validate", "obj:test"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["valid"] is True
        assert data["errors"] == []
        assert {i["code"] for i in data["info"]} == {"L002"}

    def test_root_json_merges_object_info(self, runner: CliRunner, storage_root: Path):
        _inflate_head_message(storage_root)
        result = runner.invoke(app, ["--root", str(storage_root), "--json", "validate"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["valid"] is True
        assert {i["code"] for i in data["info"]} == {"L002"}
        assert all(i["message"].startswith("obj:test: ") for i in data["info"])

    def test_object_text_shows_info(self, runner: CliRunner, storage_root: Path):
        _inflate_head_message(storage_root)
        result = runner.invoke(
            app, ["--root", str(storage_root), "validate", "obj:test"]
        )
        assert result.exit_code == 0, result.output
        assert "Info: [L002]" in result.stderr
        assert "Valid" in result.stdout
