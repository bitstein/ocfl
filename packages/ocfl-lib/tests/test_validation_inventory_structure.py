"""Tests for inventory validation — structural checks (raw dict)."""

from ocfl_lib.validation.inventory import validate_inventory
from ocfl_lib.validation.result import ValidationResult


def _minimal_inv() -> dict:
    """Return a minimal valid inventory as a raw dict."""
    return {
        "id": "http://example.org/obj1",
        "type": "https://ocfl.io/1.1/spec/#inventory",
        "digestAlgorithm": "sha512",
        "head": "v1",
        "manifest": {
            "abc123def456": ["v1/content/file.txt"],
        },
        "versions": {
            "v1": {
                "created": "2024-01-01T00:00:00Z",
                "state": {
                    "abc123def456": ["file.txt"],
                },
            },
        },
    }


class TestValidInventory:
    def test_minimal_valid_inventory(self):
        r = ValidationResult()
        inv = validate_inventory(_minimal_inv(), r, lax_digests=True)
        assert r.is_valid, [e.code for e in r.errors]
        assert inv is not None
        assert inv.id == "http://example.org/obj1"


class TestRequiredKeys:
    def test_missing_id(self):
        d = _minimal_inv()
        del d["id"]
        r = ValidationResult()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E036a")

    def test_missing_type(self):
        d = _minimal_inv()
        del d["type"]
        r = ValidationResult()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E036b")

    def test_missing_digest_algorithm(self):
        d = _minimal_inv()
        del d["digestAlgorithm"]
        r = ValidationResult()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E036c")

    def test_missing_head(self):
        d = _minimal_inv()
        del d["head"]
        r = ValidationResult()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E036d")

    def test_missing_manifest(self):
        d = _minimal_inv()
        del d["manifest"]
        r = ValidationResult()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E041a")

    def test_missing_versions(self):
        d = _minimal_inv()
        del d["versions"]
        r = ValidationResult()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E041b")


class TestUnknownKeys:
    def test_unknown_top_level_key(self):
        d = _minimal_inv()
        d["extra"] = "bad"
        r = ValidationResult()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E102")


class TestIdValidation:
    def test_empty_id(self):
        d = _minimal_inv()
        d["id"] = ""
        r = ValidationResult()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E037a")

    def test_non_string_id(self):
        d = _minimal_inv()
        d["id"] = 42
        r = ValidationResult()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E037a")

    def test_non_uri_id_warning(self):
        d = _minimal_inv()
        d["id"] = "not a uri"
        r = ValidationResult()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("W005")


class TestTypeValidation:
    def test_non_string_type(self):
        d = _minimal_inv()
        d["type"] = 42
        r = ValidationResult()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E038d")

    def test_invalid_type_url(self):
        d = _minimal_inv()
        d["type"] = "not-a-url"
        r = ValidationResult()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E038b")

    def test_unsupported_version(self):
        d = _minimal_inv()
        d["type"] = "https://ocfl.io/9.9/spec/#inventory"
        r = ValidationResult()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E038c")

    def test_valid_1_0_type(self):
        d = _minimal_inv()
        d["type"] = "https://ocfl.io/1.0/spec/#inventory"
        r = ValidationResult()
        validate_inventory(d, r, lax_digests=True)
        assert not r.has_code("E038")


class TestDigestAlgorithmValidation:
    def test_invalid_algorithm(self):
        d = _minimal_inv()
        d["digestAlgorithm"] = "md5"
        r = ValidationResult()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E025a")

    def test_sha256_warning(self):
        d = _minimal_inv()
        d["digestAlgorithm"] = "sha256"
        r = ValidationResult()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("W004")

    def test_sha512_no_warning(self):
        d = _minimal_inv()
        r = ValidationResult()
        validate_inventory(d, r, lax_digests=True)
        assert not r.has_code("W004")


class TestContentDirectory:
    def test_non_string_content_dir(self):
        d = _minimal_inv()
        d["contentDirectory"] = 42
        r = ValidationResult()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E017")

    def test_slash_in_content_dir(self):
        d = _minimal_inv()
        d["contentDirectory"] = "a/b"
        r = ValidationResult()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E017")

    def test_dot_content_dir(self):
        d = _minimal_inv()
        d["contentDirectory"] = "."
        r = ValidationResult()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E018")

    def test_dotdot_content_dir(self):
        d = _minimal_inv()
        d["contentDirectory"] = ".."
        r = ValidationResult()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E018")

    def test_valid_content_dir(self):
        d = _minimal_inv()
        d["contentDirectory"] = "data"
        d["manifest"] = {"abc123def456": ["v1/data/file.txt"]}
        r = ValidationResult()
        validate_inventory(d, r, lax_digests=True)
        assert not r.has_code("E017")
        assert not r.has_code("E018")
