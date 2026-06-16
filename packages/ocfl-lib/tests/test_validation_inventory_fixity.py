"""Tests for inventory validation — fixity checks."""

from ocfl_lib.validation.inventory import validate_inventory
from ocfl_lib.validation.result import ValidationResult

_D1 = "a" * 128


def _inv_with_fixity(fixity, *, lax_digests=False):
    """Build inventory dict with given fixity block."""
    return {
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
                "user": {"name": "Test", "address": "mailto:test@example.org"},
            },
        },
        "fixity": fixity,
    }


class TestFixityStructure:
    def test_fixity_not_object(self):
        r = ValidationResult()
        validate_inventory(_inv_with_fixity("bad"), r)
        assert r.has_code("E056a")

    def test_unknown_algorithm(self):
        r = ValidationResult()
        validate_inventory(_inv_with_fixity({"unknown-alg": {}}), r)
        assert r.has_code("E056b")

    def test_known_algorithm_accepted(self):
        r = ValidationResult()
        validate_inventory(
            _inv_with_fixity({"md5": {"d" * 32: ["v1/content/file.txt"]}}), r
        )
        assert not r.has_code("E056b")

    def test_lax_digests_accepts_unknown(self):
        r = ValidationResult()
        validate_inventory(
            _inv_with_fixity({"unknown-alg": {}}, lax_digests=True),
            r,
            lax_digests=True,
        )
        assert not r.has_code("E056b")


class TestFixityDigestMap:
    def test_value_not_object(self):
        r = ValidationResult()
        validate_inventory(_inv_with_fixity({"md5": "bad"}), r)
        assert r.has_code("E057a")

    def test_bad_digest_format(self):
        r = ValidationResult()
        validate_inventory(
            _inv_with_fixity({"md5": {"not-hex": ["v1/content/file.txt"]}}), r
        )
        assert r.has_code("E057b")

    def test_value_not_list(self):
        r = ValidationResult()
        validate_inventory(
            _inv_with_fixity({"md5": {"d" * 32: "v1/content/file.txt"}}), r
        )
        assert r.has_code("E057c")

    def test_path_not_in_manifest(self):
        r = ValidationResult()
        validate_inventory(
            _inv_with_fixity({"md5": {"d" * 32: ["v1/content/nonexistent.txt"]}}),
            r,
        )
        assert r.has_code("E057d")

    def test_path_with_invalid_segment(self):
        r = ValidationResult()
        validate_inventory(
            _inv_with_fixity({"md5": {"d" * 32: ["v1/content/../file.txt"]}}),
            r,
        )
        assert r.has_code("E099")
        assert not r.has_code("E057d")

    def test_path_with_leading_slash(self):
        r = ValidationResult()
        validate_inventory(
            _inv_with_fixity({"md5": {"d" * 32: ["/v1/content/file.txt"]}}),
            r,
        )
        assert r.has_code("E100")
        assert not r.has_code("E057d")

    def test_path_in_manifest_accepted(self):
        r = ValidationResult()
        validate_inventory(
            _inv_with_fixity({"md5": {"d" * 32: ["v1/content/file.txt"]}}), r
        )
        assert not r.has_code("E057d")


class TestFixityDuplicateDigests:
    def test_case_insensitive_duplicate(self):
        r = ValidationResult()
        validate_inventory(
            _inv_with_fixity(
                {
                    "md5": {
                        "A" * 32: ["v1/content/file.txt"],
                        "a" * 32: ["v1/content/file.txt"],
                    },
                }
            ),
            r,
        )
        assert r.has_code("E097")


class TestValidFixity:
    def test_valid_fixity_no_errors(self):
        r = ValidationResult()
        validate_inventory(
            _inv_with_fixity(
                {
                    "sha256": {"e" * 64: ["v1/content/file.txt"]},
                    "md5": {"d" * 32: ["v1/content/file.txt"]},
                }
            ),
            r,
        )
        assert r.is_valid
