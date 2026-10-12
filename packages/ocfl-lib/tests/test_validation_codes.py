"""Tests for validation codes and result."""

import pytest

from ocfl_lib.validation.codes import get_code
from ocfl_lib.validation.result import ValidationError, ValidationResult


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
        assert code.description  # still has a description


class TestValidationError:
    def test_error_construction(self):
        err = ValidationError(code="E001a", message="bad file", is_warning=False)
        assert err.code == "E001a"
        assert err.message == "bad file"
        assert not err.is_warning

    def test_warning_construction(self):
        w = ValidationError(code="W004", message="use sha512", is_warning=True)
        assert w.is_warning


class TestValidationResult:
    def test_empty_result_is_valid(self):
        r = ValidationResult()
        assert r.is_valid
        assert r.num_errors == 0
        assert r.num_warnings == 0

    def test_error_makes_invalid(self):
        r = ValidationResult()
        r.error("E001a", file="extra.txt")
        assert not r.is_valid
        assert r.num_errors == 1
        assert r.num_warnings == 0

    def test_warning_still_valid(self):
        r = ValidationResult()
        r.warning("W004", where="root")
        assert r.is_valid
        assert r.num_errors == 0
        assert r.num_warnings == 1

    def test_error_message_formatted(self):
        r = ValidationResult()
        r.error("E001a", file="mystery.dat")
        assert "mystery.dat" in r.errors[0].message

    def test_warning_message_formatted(self):
        r = ValidationResult()
        r.warning("W005", where="root", id="not-a-uri")
        assert "not-a-uri" in r.warnings[0].message

    def test_has_code_exact(self):
        r = ValidationResult()
        r.error("E001a", file="x")
        assert r.has_code("E001a")
        assert not r.has_code("E001b")

    def test_has_code_base(self):
        r = ValidationResult()
        r.error("E001a", file="x")
        r.error("E001b", dir="y")
        assert r.has_code("E001")

    def test_has_code_warnings(self):
        r = ValidationResult()
        r.warning("W004", where="root")
        assert r.has_code("W004")

    def test_multiple_errors(self):
        r = ValidationResult()
        r.error("E001a", file="a")
        r.error("E001b", dir="b")
        r.warning("W004", where="root")
        assert r.num_errors == 2
        assert r.num_warnings == 1
        assert not r.is_valid

    def test_error_no_params(self):
        r = ValidationResult()
        r.error("E063")
        assert r.num_errors == 1
        assert r.errors[0].message
