import dataclasses
from types import MappingProxyType

import pytest

from ocfl_lib.validation._codes import get_code
from ocfl_lib.validation._report import Finding, FindingCollector, Severity


class TestValidationCode:
    def test_get_known_code(self):
        code = get_code("E001a")
        assert code.code == "E001a"
        assert code.params == ["file"]
        assert "{file}" in code.description

    def test_get_warning_code(self):
        code = get_code("W004")
        assert code.code == "W004"
        assert "sha512" in code.description

    def test_get_unknown_code_raises(self):
        with pytest.raises(KeyError):
            get_code("E999z")

    def test_all_codes_have_description(self):
        for code_str in ("E001a", "E033", "E063", "W001", "W016"):
            code = get_code(code_str)
            assert code.description

    def test_code_params_match_placeholders(self):
        code = get_code("E060")
        for param in code.params:
            assert f"{{{param}}}" in code.description

    def test_format_description(self):
        code = get_code("E001a")
        msg = code.description.format(file="extra.txt")
        assert "extra.txt" in msg

    def test_no_params_code(self):
        code = get_code("E063")
        assert code.params == []
        assert code.description


class TestFinding:
    def test_render_uses_detail(self):
        f = Finding(
            code="E001a",
            severity=Severity.ERROR,
            object_id="ark:1",
            path="obj",
            detail=MappingProxyType({"file": "bad.txt"}),
        )
        assert "bad.txt" in f.render()

    def test_frozen_and_hashable(self):
        f = Finding(code="E063", severity=Severity.ERROR, object_id=None, path="")
        with pytest.raises(dataclasses.FrozenInstanceError):
            f.code = "E064"  # ty: ignore[invalid-assignment]
        assert hash(f) == hash(dataclasses.replace(f))


class TestFindingCollector:
    def test_empty_report_is_valid(self):
        r = FindingCollector().report()
        assert r.is_valid
        assert r.num_errors == 0
        assert r.num_warnings == 0

    def test_error_makes_invalid(self):
        c = FindingCollector()
        c.error("E001a", file="extra.txt")
        r = c.report()
        assert not r.is_valid
        assert r.num_errors == 1
        assert r.num_warnings == 0

    def test_warning_still_valid(self):
        c = FindingCollector()
        c.warning("W004", where="root")
        r = c.report()
        assert r.is_valid
        assert r.num_errors == 0
        assert r.num_warnings == 1

    def test_error_message_formatted(self):
        c = FindingCollector()
        c.error("E001a", file="mystery.dat")
        assert "mystery.dat" in c.errors[0].render()

    def test_warning_message_formatted(self):
        c = FindingCollector()
        c.warning("W005", where="root", id="not-a-uri")
        assert "not-a-uri" in c.warnings[0].render()

    def test_has_code_exact(self):
        c = FindingCollector()
        c.error("E001a", file="x")
        assert c.has_code("E001a")
        assert not c.has_code("E001b")

    def test_has_code_base(self):
        c = FindingCollector()
        c.error("E001a", file="x")
        c.error("E001b", dir="y")
        assert c.has_code("E001")

    def test_has_code_warnings(self):
        c = FindingCollector()
        c.warning("W004", where="root")
        assert c.has_code("W004")

    def test_multiple_errors(self):
        c = FindingCollector()
        c.error("E001a", file="a")
        c.error("E001b", dir="b")
        c.warning("W004", where="root")
        r = c.report()
        assert r.num_errors == 2
        assert r.num_warnings == 1
        assert not r.is_valid

    def test_error_no_params(self):
        c = FindingCollector()
        c.error("E063")
        assert c.report().num_errors == 1
        assert c.errors[0].render()

    def test_unknown_code_fails_fast(self):
        with pytest.raises(KeyError):
            FindingCollector().error("E999z")

    def test_object_id_and_path_stamped(self):
        c = FindingCollector(path="a/obj")
        c.error("E063")
        c.object_id = "ark:1"
        (f,) = c.report().findings
        assert (f.path, f.object_id) == ("a/obj", "ark:1")

    def test_with_findings_returns_new_report(self):
        c = FindingCollector()
        c.warning("W004", where="root")
        r = c.report()
        r2 = r.with_findings(r.findings)
        assert len(r2.findings) == 2
        assert len(r.findings) == 1
