"""Tests for inventory validation — manifest checks."""

import unicodedata

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


class TestManifestTraversalGaps:
    """Adversarial path traversal — verify existing checks cover gaps."""

    def test_absolute_path_rejected(self):
        r = ValidationResult()
        validate_inventory(_inv_with_manifest({_D1: ["/etc/passwd"]}), r)
        assert r.has_code("E100")

    def test_backslash_treated_as_literal(self):
        # Characterization: backslash is a valid filename char on POSIX,
        # not a path separator. Accepted as part of segment.
        r = ValidationResult()
        validate_inventory(
            _inv_with_manifest({_D1: ["v1/content/foo\\bar"]}), r
        )
        assert not r.has_code("E099")
        assert not r.has_code("E100")

    def test_url_encoded_dotdot_treated_as_literal(self):
        # Characterization: %2e%2e is a literal segment, not decoded.
        r = ValidationResult()
        validate_inventory(
            _inv_with_manifest({_D1: ["v1/content/%2e%2e/file.txt"]}), r
        )
        assert not r.has_code("E099")


class TestManifestNullBytes:
    def test_null_byte_in_manifest_path(self):
        r = ValidationResult()
        validate_inventory(
            _inv_with_manifest({_D1: ["v1/content/foo\x00bar"]}), r
        )
        assert r.has_code("L001")

    def test_null_byte_in_logical_state_path(self):
        r = ValidationResult()
        versions = {
            "v1": {
                "created": "2024-01-01T00:00:00Z",
                "state": {_D1: ["foo\x00bar.txt"]},
            },
        }
        validate_inventory(
            _inv_with_manifest(
                {_D1: ["v1/content/file.txt"]}, versions=versions
            ),
            r,
        )
        assert r.has_code("L001")

    def test_null_byte_in_object_id(self):
        r = ValidationResult()
        inv = _inv_with_manifest({_D1: ["v1/content/file.txt"]})
        inv["id"] = "http://example.org/obj\x00evil"
        validate_inventory(inv, r)
        assert r.has_code("L001")


class TestAbsurdLengthCaps:
    def test_object_id_over_1kb(self):
        r = ValidationResult()
        inv = _inv_with_manifest({_D1: ["v1/content/file.txt"]})
        inv["id"] = "http://example.org/" + ("x" * 2048)
        validate_inventory(inv, r)
        assert r.has_code("L002")

    def test_manifest_path_over_4kb(self):
        r = ValidationResult()
        long_seg = "x" * 5000
        validate_inventory(
            _inv_with_manifest({_D1: [f"v1/content/{long_seg}"]}), r
        )
        assert r.has_code("L002")

    def test_logical_path_over_4kb(self):
        r = ValidationResult()
        long_seg = "x" * 5000
        versions = {
            "v1": {
                "created": "2024-01-01T00:00:00Z",
                "state": {_D1: [long_seg]},
            },
        }
        validate_inventory(
            _inv_with_manifest(
                {_D1: ["v1/content/file.txt"]}, versions=versions
            ),
            r,
        )
        assert r.has_code("L002")

    def test_message_over_64kb(self):
        r = ValidationResult()
        versions = {
            "v1": {
                "created": "2024-01-01T00:00:00Z",
                "state": {_D1: ["file.txt"]},
                "message": "x" * (64 * 1024 + 1),
            },
        }
        validate_inventory(
            _inv_with_manifest(
                {_D1: ["v1/content/file.txt"]}, versions=versions
            ),
            r,
        )
        assert r.has_code("L002")

    def test_user_name_over_1kb(self):
        r = ValidationResult()
        versions = {
            "v1": {
                "created": "2024-01-01T00:00:00Z",
                "state": {_D1: ["file.txt"]},
                "user": {"name": "x" * 2048, "address": "mailto:a@b.c"},
            },
        }
        validate_inventory(
            _inv_with_manifest(
                {_D1: ["v1/content/file.txt"]}, versions=versions
            ),
            r,
        )
        assert r.has_code("L002")


class TestUnicodeNormalizationCharacterization:
    """Pin current behavior: NFC and NFD are treated as DISTINCT.

    TODO: revisit policy — see REVIEW_PLAN.md. Other OCFL impls (ocfl-go,
    ocfl-py, gocfl, rocfl) all do byte-exact compare; matching the field.
    """

    def test_nfc_and_nfd_treated_as_distinct_paths(self):
        nfc = unicodedata.normalize("NFC", "café.txt")
        nfd = unicodedata.normalize("NFD", "café.txt")
        assert nfc != nfd
        r = ValidationResult()
        manifest = {
            _D1: [f"v1/content/{nfc}"],
            _D2: [f"v1/content/{nfd}"],
        }
        versions = {
            "v1": {
                "created": "2024-01-01T00:00:00Z",
                "state": {_D1: [nfc], _D2: [nfd]},
            },
        }
        validate_inventory(_inv_with_manifest(manifest, versions=versions), r)
        # Currently no duplicate detected — pinning this behavior.
        assert not r.has_code("E101a")
        assert not r.has_code("E095a")
