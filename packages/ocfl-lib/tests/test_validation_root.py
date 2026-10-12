"""Tests for OCFL storage root validation."""

import hashlib
import json
from typing import TYPE_CHECKING

from ocfl_lib.fs.local import LocalFS

if TYPE_CHECKING:
    from pathlib import Path
from ocfl_lib.validation.report import ValidationReport
from ocfl_lib.validation.root import validate_root


class TestValidRoot:
    def test_minimal_root(self, tmp_path: Path):
        """Create a minimal valid storage root and validate it."""
        # NAMASTE
        (tmp_path / "0=ocfl_1.1").write_text("ocfl_1.1\n")
        # A single valid object
        obj = tmp_path / "obj1"
        obj.mkdir()
        (obj / "0=ocfl_object_1.1").write_text("ocfl_object_1.1\n")
        inv = {
            "id": "http://example.org/obj1",
            "type": "https://ocfl.io/1.1/spec/#inventory",
            "digestAlgorithm": "sha512",
            "head": "v1",
            "manifest": {},
            "versions": {
                "v1": {
                    "created": "2024-01-01T00:00:00Z",
                    "state": {},
                    "message": "init",
                    "user": {"name": "test", "address": "mailto:t@e.org"},
                },
            },
        }
        inv_bytes = json.dumps(inv, sort_keys=True, separators=(",", ":")).encode()
        (obj / "inventory.json").write_bytes(inv_bytes)
        digest = hashlib.sha512(inv_bytes).hexdigest()
        (obj / "inventory.json.sha512").write_text(f"{digest} inventory.json\n")
        v1 = obj / "v1"
        v1.mkdir()
        (v1 / "inventory.json").write_bytes(inv_bytes)
        (v1 / "inventory.json.sha512").write_text(f"{digest} inventory.json\n")

        fsys = LocalFS(tmp_path)
        r = validate_root(fsys, "", validate_objects=False)
        assert r.is_valid, [(e.code, e.render()) for e in r.errors]
        assert isinstance(r, ValidationReport)


class TestRootNamaste:
    def test_missing_namaste(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        r = validate_root(fsys, "", validate_objects=False)
        assert r.has_code("E069")

    def test_valid_namaste(self, tmp_path: Path):
        (tmp_path / "0=ocfl_1.1").write_text("ocfl_1.1\n")
        fsys = LocalFS(tmp_path)
        r = validate_root(fsys, "", validate_objects=False)
        assert not r.has_code("E069")


class TestRootExtensions:
    def test_non_dir_in_extensions(self, tmp_path: Path):
        (tmp_path / "0=ocfl_1.1").write_text("ocfl_1.1\n")
        ext = tmp_path / "extensions"
        ext.mkdir()
        (ext / "somefile.txt").write_text("bad")
        fsys = LocalFS(tmp_path)
        r = validate_root(fsys, "", validate_objects=False)
        assert r.has_code("E112")
        assert not r.has_code("E086")

    def test_unregistered_extension_warning(self, tmp_path: Path):
        (tmp_path / "0=ocfl_1.1").write_text("ocfl_1.1\n")
        ext = tmp_path / "extensions"
        ext.mkdir()
        (ext / "9999-custom-extension").mkdir()
        fsys = LocalFS(tmp_path)
        r = validate_root(fsys, "", validate_objects=False)
        assert r.has_code("W016")
        assert not r.has_code("W901")


class TestE069cUnparseableDecl:
    def test_unparseable_zero_eq_fires_E069c(self, tmp_path: Path):
        """Root dir has 0= files but none parse as valid root NAMASTE -> E069c."""
        (tmp_path / "0=garbage_1.1").write_text("garbage\n")
        fsys = LocalFS(tmp_path)
        r = validate_root(fsys, "", validate_objects=False)
        assert r.has_code("E069c")
        assert not any(e.code == "E069a" for e in r.errors)

    def test_no_zero_eq_at_all_fires_E069a(self, tmp_path: Path):
        """Root dir has no 0= files at all -> E069a (not E069c)."""
        (tmp_path / "readme.txt").write_text("hello\n")
        fsys = LocalFS(tmp_path)
        r = validate_root(fsys, "", validate_objects=False)
        assert any(e.code == "E069a" for e in r.errors)
        assert not r.has_code("E069c")


class TestE069dInvalidNamaste:
    def test_unparseable_zero_eq_fires_E069d(self, tmp_path: Path):
        """Each unparseable 0= file fires E069d with filename."""
        (tmp_path / "0=garbage_1.1").write_text("garbage\n")
        (tmp_path / "0=alsogarbage").write_text("nope\n")
        fsys = LocalFS(tmp_path)
        r = validate_root(fsys, "", validate_objects=False)
        e069d_errors = [e for e in r.errors if e.code == "E069d"]
        msgs = [e.render() for e in e069d_errors]
        assert any("0=garbage_1.1" in m for m in msgs)
        assert any("0=alsogarbage" in m for m in msgs)

    def test_valid_plus_unparseable_fires_E069d_for_extra(self, tmp_path: Path):
        """Valid root decl + extra unparseable 0= file -> E069d for the extra."""
        (tmp_path / "0=ocfl_1.1").write_text("ocfl_1.1\n")
        (tmp_path / "0=garbage").write_text("nope\n")
        fsys = LocalFS(tmp_path)
        r = validate_root(fsys, "", validate_objects=False)
        e069d_errors = [e for e in r.errors if e.code == "E069d"]
        assert len(e069d_errors) == 1
        assert "0=garbage" in e069d_errors[0].render()


class TestE003dMultipleDeclarations:
    def test_multiple_zero_eq_files_emits_E003d(self, tmp_path: Path):
        (tmp_path / "0=ocfl_1.1").write_text("ocfl_1.1\n")
        subdir = tmp_path / "obj1"
        subdir.mkdir()
        (subdir / "0=ocfl_object_1.0").write_text("ocfl_object_1.0\n")
        (subdir / "0=ocfl_object_1.1").write_text("ocfl_object_1.1\n")
        fsys = LocalFS(tmp_path)
        r = validate_root(fsys, "", validate_objects=True)
        assert r.has_code("E003d")


class TestE004aUnknownVersion:
    def test_unknown_version_emits_E004a(self, tmp_path: Path):
        (tmp_path / "0=ocfl_1.1").write_text("ocfl_1.1\n")
        subdir = tmp_path / "obj1"
        subdir.mkdir()
        (subdir / "0=ocfl_object_0.9").write_text("ocfl_object_0.9\n")
        fsys = LocalFS(tmp_path)
        r = validate_root(fsys, "", validate_objects=True)
        assert r.has_code("E004a")


class TestE004bMalformedDeclaration:
    def test_malformed_declaration_emits_E004b(self, tmp_path: Path):
        (tmp_path / "0=ocfl_1.1").write_text("ocfl_1.1\n")
        subdir = tmp_path / "obj1"
        subdir.mkdir()
        (subdir / "0=special_object_yeah").write_text("junk\n")
        fsys = LocalFS(tmp_path)
        r = validate_root(fsys, "", validate_objects=True)
        assert r.has_code("E004b")

    def test_bare_zero_eq_emits_E004b(self, tmp_path: Path):
        (tmp_path / "0=ocfl_1.1").write_text("ocfl_1.1\n")
        subdir = tmp_path / "obj1"
        subdir.mkdir()
        (subdir / "0=foo").write_text("foo\n")
        fsys = LocalFS(tmp_path)
        r = validate_root(fsys, "", validate_objects=True)
        assert r.has_code("E004b")


class TestE073EmptyDir:
    def test_empty_dir_in_hierarchy_emits_E073(self, tmp_path: Path):
        (tmp_path / "0=ocfl_1.1").write_text("ocfl_1.1\n")
        (tmp_path / "empty_dir").mkdir()
        fsys = LocalFS(tmp_path)
        r = validate_root(fsys, "", validate_objects=True)
        assert r.has_code("E073")

    def test_no_empty_dirs_no_E073(self, tmp_path: Path):
        (tmp_path / "0=ocfl_1.1").write_text("ocfl_1.1\n")
        fsys = LocalFS(tmp_path)
        r = validate_root(fsys, "", validate_objects=True)
        assert not r.has_code("E073")


class TestE072FilesInHierarchy:
    def test_files_in_non_object_dir_emits_E072(self, tmp_path: Path):
        (tmp_path / "0=ocfl_1.1").write_text("ocfl_1.1\n")
        subdir = tmp_path / "some_dir"
        subdir.mkdir()
        (subdir / "stray.txt").write_text("bad")
        fsys = LocalFS(tmp_path)
        r = validate_root(fsys, "", validate_objects=True)
        assert r.has_code("E072")


class TestRootSummary:
    def test_summary_counts(self, tmp_path: Path):
        (tmp_path / "0=ocfl_1.1").write_text("ocfl_1.1\n")
        fsys = LocalFS(tmp_path)
        summary = validate_root(fsys, "", validate_objects=False)
        assert summary.objects_checked == 0
        assert summary.objects_valid == 0
        assert summary.truncated is False

    def test_truncated_flag(self, tmp_path: Path):
        (tmp_path / "0=ocfl_1.1").write_text("ocfl_1.1\n")
        # Create 3 invalid objects (just NAMASTE, no inventory)
        for i in range(3):
            d = tmp_path / f"obj{i}"
            d.mkdir()
            (d / "0=ocfl_object_1.1").write_text("ocfl_object_1.1\n")
        fsys = LocalFS(tmp_path)
        summary = validate_root(fsys, "", max_errors=2)
        assert summary.truncated is True
        assert len({f.path for f in summary.errors}) == 2
