"""Tests for inventory validation — manifest checks."""

from ocfl_lib.validation.inventory import validate_inventory
from ocfl_lib.validation.result import ValidationResult

# sha512 hex digest (128 chars)
_D1 = "a" * 128
_D2 = "b" * 128
_D3 = "c" * 128


def _inv_with_manifest(manifest, *, alg="sha512", content_dir="content", versions=None):
    """Build inventory dict with given manifest."""
    if versions is None:
        # Default: v1 state references first digest in manifest
        first_digest = next(iter(manifest)) if manifest else _D1
        versions = {
            "v1": {
                "created": "2024-01-01T00:00:00Z",
                "state": {first_digest: ["file.txt"]},
            },
        }
    return {
        "id": "http://example.org/obj1",
        "type": "https://ocfl.io/1.1/spec/#inventory",
        "digestAlgorithm": alg,
        "head": "v1",
        "manifest": manifest,
        "versions": versions,
    }


class TestManifestDigestFormat:
    def test_invalid_digest_format(self):
        r = ValidationResult()
        validate_inventory(
            _inv_with_manifest({"not-a-hex-digest": ["v1/content/file.txt"]}), r
        )
        assert r.has_code("E025b")

    def test_wrong_length_digest(self):
        r = ValidationResult()
        validate_inventory(_inv_with_manifest({"abcdef": ["v1/content/file.txt"]}), r)
        assert r.has_code("E025b")

    def test_valid_digest_no_error(self):
        r = ValidationResult()
        validate_inventory(_inv_with_manifest({_D1: ["v1/content/file.txt"]}), r)
        assert not r.has_code("E025b")


class TestManifestDuplicateDigests:
    def test_case_insensitive_duplicate(self):
        r = ValidationResult()
        manifest = {
            "A" * 128: ["v1/content/a.txt"],
            "a" * 128: ["v1/content/b.txt"],
        }
        validate_inventory(_inv_with_manifest(manifest), r)
        assert r.has_code("E096")


class TestManifestValues:
    def test_value_not_array(self):
        r = ValidationResult()
        validate_inventory(_inv_with_manifest({_D1: "v1/content/file.txt"}), r)
        assert r.has_code("E092") or r.has_code("E091")

    def test_value_array_non_string(self):
        r = ValidationResult()
        validate_inventory(_inv_with_manifest({_D1: [42]}), r)
        assert r.has_code("E092") or r.has_code("E091")


class TestManifestContentPaths:
    def test_path_must_start_with_version(self):
        r = ValidationResult()
        validate_inventory(_inv_with_manifest({_D1: ["content/file.txt"]}), r)
        assert r.has_code("E042")

    def test_path_invalid_version_dir(self):
        r = ValidationResult()
        validate_inventory(_inv_with_manifest({_D1: ["vX/content/file.txt"]}), r)
        assert r.has_code("E042")

    def test_path_wrong_content_dir(self):
        r = ValidationResult()
        validate_inventory(_inv_with_manifest({_D1: ["v1/wrong/file.txt"]}), r)
        assert r.has_code("E042")

    def test_valid_content_path(self):
        r = ValidationResult()
        validate_inventory(_inv_with_manifest({_D1: ["v1/content/file.txt"]}), r)
        assert not r.has_code("E042")


class TestManifestDuplicatePaths:
    def test_duplicate_content_path(self):
        r = ValidationResult()
        manifest = {
            _D1: ["v1/content/file.txt"],
            _D2: ["v1/content/file.txt"],
        }
        versions = {
            "v1": {
                "created": "2024-01-01T00:00:00Z",
                "state": {_D1: ["a.txt"], _D2: ["b.txt"]},
            },
        }
        validate_inventory(_inv_with_manifest(manifest, versions=versions), r)
        assert r.has_code("E101")


class TestManifestPathConflicts:
    def test_path_prefix_conflict(self):
        r = ValidationResult()
        manifest = {
            _D1: ["v1/content/dir/file.txt"],
            _D2: ["v1/content/dir"],
        }
        versions = {
            "v1": {
                "created": "2024-01-01T00:00:00Z",
                "state": {_D1: ["a.txt"], _D2: ["b.txt"]},
            },
        }
        validate_inventory(_inv_with_manifest(manifest, versions=versions), r)
        assert r.has_code("E101") or r.has_code("E095")


class TestManifestPathSegments:
    def test_dot_segment(self):
        r = ValidationResult()
        validate_inventory(_inv_with_manifest({_D1: ["v1/content/./file.txt"]}), r)
        assert r.has_code("E099")

    def test_dotdot_segment(self):
        r = ValidationResult()
        validate_inventory(_inv_with_manifest({_D1: ["v1/content/../file.txt"]}), r)
        assert r.has_code("E099")

    def test_empty_segment(self):
        r = ValidationResult()
        validate_inventory(_inv_with_manifest({_D1: ["v1/content//file.txt"]}), r)
        assert r.has_code("E099")

    def test_leading_slash(self):
        r = ValidationResult()
        validate_inventory(_inv_with_manifest({_D1: ["/v1/content/file.txt"]}), r)
        assert r.has_code("E100")

    def test_trailing_slash(self):
        r = ValidationResult()
        validate_inventory(_inv_with_manifest({_D1: ["v1/content/file.txt/"]}), r)
        assert r.has_code("E100")
