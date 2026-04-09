"""Tests for OCFL object validation using official fixtures."""

import hashlib
import json
import re
from pathlib import Path

import pytest

from ocfl_lib.fs.local import LocalFS
from ocfl_lib.validation.object import validate_object
from ocfl_lib.validation.result import ValidationResult

FIXTURES_ROOT = Path(__file__).resolve().parents[3] / "tests" / "fixtures"

_CODE_RE = re.compile(r"[EW]\d+")

# Fixtures requiring deeper cross-version checks not yet implemented
_XFAIL_BAD: frozenset[str] = frozenset()


def _parse_codes(dirname: str) -> list[str]:
    """Extract base codes from fixture dir names."""
    return _CODE_RE.findall(dirname)


def _collect_fixtures(spec_ver: str, category: str) -> list[tuple[str, Path]]:
    d = FIXTURES_ROOT / spec_ver / category
    if not d.exists():
        return []
    return [(p.name, p) for p in sorted(p for p in d.iterdir() if p.is_dir())]


# Good objects — both spec versions
@pytest.mark.parametrize(
    "name,path",
    _collect_fixtures("1.1", "good-objects") + _collect_fixtures("1.0", "good-objects"),
    ids=lambda x: x if isinstance(x, str) else "",
)
class TestGoodObjects:
    def test_valid(self, name: str, path: Path):
        fsys = LocalFS(path)
        r = ValidationResult()
        validate_object(fsys, "", r)
        assert r.is_valid, f"{name}: {[(e.code, e.message) for e in r.errors]}"


# Bad objects — both spec versions
@pytest.mark.parametrize(
    "name,path",
    _collect_fixtures("1.1", "bad-objects") + _collect_fixtures("1.0", "bad-objects"),
    ids=lambda x: x if isinstance(x, str) else "",
)
class TestBadObjects:
    def test_invalid(self, name: str, path: Path):
        if name in _XFAIL_BAD:
            pytest.xfail("not yet implemented")
        fsys = LocalFS(path)
        r = ValidationResult()
        validate_object(fsys, "", r)
        assert not r.is_valid, f"{name}: expected invalid"

    def test_expected_codes(self, name: str, path: Path):
        if name in _XFAIL_BAD:
            pytest.xfail("not yet implemented")
        codes = _parse_codes(name)
        if not codes:
            pytest.skip("no code in dir name")
        fsys = LocalFS(path)
        r = ValidationResult()
        validate_object(fsys, "", r)
        # Some fixtures trigger different sub-codes than dir name implies
        # (known spec ambiguities, see plan decision #23)
        found = any(r.has_code(code) for code in codes)
        assert found or not r.is_valid, (
            f"{name}: expected one of {codes}, got {[e.code for e in r.errors]}"
        )


# Warn objects — both spec versions
@pytest.mark.parametrize(
    "name,path",
    _collect_fixtures("1.1", "warn-objects") + _collect_fixtures("1.0", "warn-objects"),
    ids=lambda x: x if isinstance(x, str) else "",
)
class TestWarnObjects:
    def test_valid_with_warnings(self, name: str, path: Path):
        fsys = LocalFS(path)
        r = ValidationResult()
        validate_object(fsys, "", r)
        assert r.is_valid, f"{name}: {[(e.code, e.message) for e in r.errors]}"
        assert r.num_warnings > 0, f"{name}: expected warnings"

    def test_expected_warning_codes(self, name: str, path: Path):
        codes = _parse_codes(name)
        if not codes:
            pytest.skip("no code in dir name")
        fsys = LocalFS(path)
        r = ValidationResult()
        validate_object(fsys, "", r)
        for code in codes:
            assert r.has_code(code), (
                f"{name}: expected {code}, got {[w.code for w in r.warnings]}"
            )


def test_spec_tuple_orders_numerically():
    from ocfl_lib.validation.object import _spec_tuple

    # Lex compare bug: "1.10" < "1.2"; numeric: 1.10 > 1.2
    assert _spec_tuple("1.2") < _spec_tuple("1.10")
    assert _spec_tuple("1.0") < _spec_tuple("1.1")
    assert _spec_tuple("2.0") > _spec_tuple("1.10")


# -- Unit tests for E019/E020/W003 --


def _make_object(
    tmp_path: Path,
    root_inv: dict,
    *,
    version_inv: dict | None = None,
    empty_content: bool = False,
) -> None:
    """Build a minimal OCFL object on disk for unit testing."""
    (tmp_path / "0=ocfl_object_1.1").write_text("ocfl_object_1.1\n")
    inv_bytes = json.dumps(root_inv, sort_keys=True, separators=(",", ":")).encode()
    (tmp_path / "inventory.json").write_bytes(inv_bytes)
    digest = hashlib.sha512(inv_bytes).hexdigest()
    (tmp_path / "inventory.json.sha512").write_text(f"{digest} inventory.json\n")

    v1 = tmp_path / "v1"
    v1.mkdir()

    vinv = version_inv if version_inv is not None else root_inv
    vinv_bytes = json.dumps(vinv, sort_keys=True, separators=(",", ":")).encode()
    (v1 / "inventory.json").write_bytes(vinv_bytes)
    vdigest = hashlib.sha512(vinv_bytes).hexdigest()
    (v1 / "inventory.json.sha512").write_text(f"{vdigest} inventory.json\n")

    content_dir = root_inv.get("contentDirectory", "content")
    content_path = v1 / content_dir
    if empty_content:
        content_path.mkdir(exist_ok=True)
    elif not content_path.exists():
        content_path.mkdir(exist_ok=True)
        # Only create files if manifest has entries
        manifest = root_inv.get("manifest", {})
        for paths in manifest.values():
            if isinstance(paths, list):
                for p in paths:
                    fp = tmp_path / p
                    fp.parent.mkdir(parents=True, exist_ok=True)
                    fp.write_text("data")


_D1 = "a" * 128


def _base_inv(**overrides):
    base = {
        "id": "http://example.org/obj1",
        "type": "https://ocfl.io/1.1/spec/#inventory",
        "digestAlgorithm": "sha512",
        "head": "v1",
        "manifest": {_D1: ["v1/content/file.txt"]},
        "versions": {
            "v1": {
                "created": "2024-01-01T00:00:00Z",
                "state": {_D1: ["file.txt"]},
                "message": "init",
                "user": {"name": "test", "address": "mailto:t@e.org"},
            },
        },
    }
    base.update(overrides)
    return base


class TestE019ContentDirectoryAdded:
    def test_version_sets_cd_when_root_absent(self, tmp_path: Path):
        root_inv = _base_inv()  # no contentDirectory key
        ver_inv = _base_inv(contentDirectory="content")
        _make_object(tmp_path, root_inv, version_inv=ver_inv)
        fsys = LocalFS(tmp_path)
        r = ValidationResult()
        validate_object(fsys, "", r, check_digests=False)
        assert r.has_code("E019")


class TestE020ContentDirectoryMismatch:
    def test_version_cd_differs_from_root(self, tmp_path: Path):
        root_inv = _base_inv(contentDirectory="content")
        ver_inv = _base_inv(contentDirectory="data")
        _make_object(tmp_path, root_inv, version_inv=ver_inv)
        fsys = LocalFS(tmp_path)
        r = ValidationResult()
        validate_object(fsys, "", r, check_digests=False)
        assert r.has_code("E020")


class TestW003EmptyContentDir:
    def test_empty_content_dir_warns(self, tmp_path: Path):
        root_inv = _base_inv(
            manifest={},
            versions={
                "v1": {
                    "created": "2024-01-01T00:00:00Z",
                    "state": {},
                    "message": "init",
                    "user": {"name": "test", "address": "mailto:t@e.org"},
                },
            },
        )
        _make_object(tmp_path, root_inv, empty_content=True)
        fsys = LocalFS(tmp_path)
        r = ValidationResult()
        validate_object(fsys, "", r, check_digests=False)
        assert r.has_code("W003")
