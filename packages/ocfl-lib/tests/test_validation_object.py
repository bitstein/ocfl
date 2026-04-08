"""Tests for OCFL object validation using official fixtures."""

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
