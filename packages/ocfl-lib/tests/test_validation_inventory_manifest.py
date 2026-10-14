import unicodedata

from digests import D1, D2, D3

from ocfl_lib.validation._inventory import validate_inventory
from ocfl_lib.validation._report import FindingCollector


def _inv_with_manifest(manifest, *, alg="sha512", content_dir="content", versions=None):
    """Build inventory dict with given manifest."""
    if versions is None:
        # Default: v1 state references first digest in manifest
        first_digest = next(iter(manifest)) if manifest else D1
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
        r = FindingCollector()
        validate_inventory(
            _inv_with_manifest({"not-a-hex-digest": ["v1/content/file.txt"]}), r
        )
        assert r.has_code("E025b")

    def test_wrong_length_digest(self):
        r = FindingCollector()
        validate_inventory(_inv_with_manifest({"abcdef": ["v1/content/file.txt"]}), r)
        assert r.has_code("E025b")

    def test_valid_digest_no_error(self):
        r = FindingCollector()
        validate_inventory(_inv_with_manifest({D1: ["v1/content/file.txt"]}), r)
        assert not r.has_code("E025b")


class TestManifestDuplicateDigests:
    def test_case_insensitive_duplicate(self):
        r = FindingCollector()
        manifest = {
            D1.upper(): ["v1/content/a.txt"],
            D1: ["v1/content/b.txt"],
        }
        validate_inventory(_inv_with_manifest(manifest), r)
        assert r.has_code("E096")


class TestManifestValues:
    def test_value_not_array(self):
        r = FindingCollector()
        validate_inventory(_inv_with_manifest({D1: "v1/content/file.txt"}), r)
        assert r.has_code("E092") or r.has_code("E091")

    def test_value_array_non_string(self):
        r = FindingCollector()
        validate_inventory(_inv_with_manifest({D1: [42]}), r)
        assert r.has_code("E092") or r.has_code("E091")


class TestManifestContentPaths:
    def test_path_must_start_with_version(self):
        r = FindingCollector()
        validate_inventory(_inv_with_manifest({D1: ["content/file.txt"]}), r)
        assert r.has_code("E042")

    def test_path_invalid_version_dir(self):
        r = FindingCollector()
        validate_inventory(_inv_with_manifest({D1: ["vX/content/file.txt"]}), r)
        assert r.has_code("E042")

    def test_path_wrong_content_dir(self):
        r = FindingCollector()
        validate_inventory(_inv_with_manifest({D1: ["v1/wrong/file.txt"]}), r)
        assert r.has_code("E042")

    def test_valid_content_path(self):
        r = FindingCollector()
        validate_inventory(_inv_with_manifest({D1: ["v1/content/file.txt"]}), r)
        assert not r.has_code("E042")


class TestManifestDuplicatePaths:
    def test_duplicate_content_path(self):
        r = FindingCollector()
        manifest = {
            D1: ["v1/content/file.txt"],
            D2: ["v1/content/file.txt"],
        }
        versions = {
            "v1": {
                "created": "2024-01-01T00:00:00Z",
                "state": {D1: ["a.txt"], D2: ["b.txt"]},
            },
        }
        validate_inventory(_inv_with_manifest(manifest, versions=versions), r)
        assert r.has_code("E101")


class TestManifestPathConflicts:
    def test_path_prefix_conflict(self):
        r = FindingCollector()
        manifest = {
            D1: ["v1/content/dir/file.txt"],
            D2: ["v1/content/dir"],
        }
        versions = {
            "v1": {
                "created": "2024-01-01T00:00:00Z",
                "state": {D1: ["a.txt"], D2: ["b.txt"]},
            },
        }
        validate_inventory(_inv_with_manifest(manifest, versions=versions), r)
        assert r.has_code("E101") or r.has_code("E095")

    def test_non_adjacent_path_prefix_conflict(self):
        r = FindingCollector()
        manifest = {
            D1: ["v1/content/a"],
            D2: ["v1/content/a!"],
            D3: ["v1/content/a/b"],
        }
        versions = {
            "v1": {
                "created": "2024-01-01T00:00:00Z",
                "state": {D1: ["a"], D2: ["a!"], D3: ["b"]},
            },
        }
        validate_inventory(_inv_with_manifest(manifest, versions=versions), r)
        assert r.has_code("E101b")


class TestManifestPathSegments:
    def test_dot_segment(self):
        r = FindingCollector()
        validate_inventory(_inv_with_manifest({D1: ["v1/content/./file.txt"]}), r)
        assert r.has_code("E099")

    def test_dotdot_segment(self):
        r = FindingCollector()
        validate_inventory(_inv_with_manifest({D1: ["v1/content/../file.txt"]}), r)
        assert r.has_code("E099")

    def test_empty_segment(self):
        r = FindingCollector()
        validate_inventory(_inv_with_manifest({D1: ["v1/content//file.txt"]}), r)
        assert r.has_code("E099")

    def test_leading_slash(self):
        r = FindingCollector()
        validate_inventory(_inv_with_manifest({D1: ["/v1/content/file.txt"]}), r)
        assert r.has_code("E100")

    def test_trailing_slash(self):
        r = FindingCollector()
        validate_inventory(_inv_with_manifest({D1: ["v1/content/file.txt/"]}), r)
        assert r.has_code("E100")


class TestManifestTraversalGaps:
    def test_absolute_path_rejected(self):
        r = FindingCollector()
        validate_inventory(_inv_with_manifest({D1: ["/etc/passwd"]}), r)
        assert r.has_code("E100")

    def test_backslash_treated_as_literal(self):
        r = FindingCollector()
        validate_inventory(_inv_with_manifest({D1: ["v1/content/foo\\bar"]}), r)
        assert not r.has_code("E099")
        assert not r.has_code("E100")

    def test_url_encoded_dotdot_treated_as_literal(self):
        r = FindingCollector()
        validate_inventory(_inv_with_manifest({D1: ["v1/content/%2e%2e/file.txt"]}), r)
        assert not r.has_code("E099")


class TestManifestNullBytes:
    def test_null_byte_in_manifest_path(self):
        r = FindingCollector()
        validate_inventory(_inv_with_manifest({D1: ["v1/content/foo\x00bar"]}), r)
        assert r.has_code("L001")

    def test_null_byte_in_logical_state_path(self):
        r = FindingCollector()
        versions = {
            "v1": {
                "created": "2024-01-01T00:00:00Z",
                "state": {D1: ["foo\x00bar.txt"]},
            },
        }
        validate_inventory(
            _inv_with_manifest({D1: ["v1/content/file.txt"]}, versions=versions),
            r,
        )
        assert r.has_code("L001")

    def test_null_byte_in_object_id(self):
        r = FindingCollector()
        inv = _inv_with_manifest({D1: ["v1/content/file.txt"]})
        inv["id"] = "http://example.org/obj\x00evil"
        validate_inventory(inv, r)
        assert r.has_code("L001")


class TestAbsurdLengthCaps:
    def test_object_id_over_1kb(self):
        r = FindingCollector()
        inv = _inv_with_manifest({D1: ["v1/content/file.txt"]})
        inv["id"] = "http://example.org/" + ("x" * 2048)
        validate_inventory(inv, r)
        assert r.has_code("L002")

    def test_manifest_path_over_4kb(self):
        r = FindingCollector()
        long_seg = "x" * 5000
        validate_inventory(_inv_with_manifest({D1: [f"v1/content/{long_seg}"]}), r)
        assert r.has_code("L002")

    def test_logical_path_over_4kb(self):
        r = FindingCollector()
        long_seg = "x" * 5000
        versions = {
            "v1": {
                "created": "2024-01-01T00:00:00Z",
                "state": {D1: [long_seg]},
            },
        }
        validate_inventory(
            _inv_with_manifest({D1: ["v1/content/file.txt"]}, versions=versions),
            r,
        )
        assert r.has_code("L002")

    def test_message_over_64kb(self):
        r = FindingCollector()
        versions = {
            "v1": {
                "created": "2024-01-01T00:00:00Z",
                "state": {D1: ["file.txt"]},
                "message": "x" * (64 * 1024 + 1),
            },
        }
        validate_inventory(
            _inv_with_manifest({D1: ["v1/content/file.txt"]}, versions=versions),
            r,
        )
        assert r.has_code("L002")

    def test_user_name_over_1kb(self):
        r = FindingCollector()
        versions = {
            "v1": {
                "created": "2024-01-01T00:00:00Z",
                "state": {D1: ["file.txt"]},
                "user": {"name": "x" * 2048, "address": "mailto:a@b.c"},
            },
        }
        validate_inventory(
            _inv_with_manifest({D1: ["v1/content/file.txt"]}, versions=versions),
            r,
        )
        assert r.has_code("L002")


def _info_codes(r: FindingCollector) -> list[str]:
    return [i.code for i in r.infos]


def _error_codes(r: FindingCollector) -> list[str]:
    return [e.code for e in r.errors]


class TestLimitsAreInfoOnly:
    def test_long_object_id_is_valid(self):
        r = FindingCollector()
        inv = _inv_with_manifest({D1: ["v1/content/file.txt"]})
        inv["id"] = "http://example.org/" + ("x" * 2048)
        validate_inventory(inv, r)
        assert _info_codes(r) == ["L002"]
        assert r.is_valid

    def test_null_in_object_id_is_valid(self):
        r = FindingCollector()
        inv = _inv_with_manifest({D1: ["v1/content/file.txt"]})
        inv["id"] = "http://example.org/obj\x00x"
        validate_inventory(inv, r)
        assert _info_codes(r) == ["L001"]
        assert r.is_valid

    def test_null_manifest_path_still_spec_checked(self):
        r = FindingCollector()
        validate_inventory(_inv_with_manifest({D1: ["/v1/content/a\x00b"]}), r)
        assert "L001" in _info_codes(r)
        assert "E100" in _error_codes(r)

    def test_long_manifest_path_still_spec_checked(self):
        r = FindingCollector()
        long_seg = "x" * 5000
        validate_inventory(_inv_with_manifest({D1: [f"v1/content//{long_seg}"]}), r)
        assert "L002" in _info_codes(r)
        assert "E099" in _error_codes(r)

    def test_null_logical_path_still_spec_checked(self):
        r = FindingCollector()
        versions = {
            "v1": {
                "created": "2024-01-01T00:00:00Z",
                "state": {D1: ["/a\x00b"]},
            },
        }
        validate_inventory(
            _inv_with_manifest({D1: ["v1/content/file.txt"]}, versions=versions),
            r,
        )
        assert "L001" in _info_codes(r)
        assert "E053" in _error_codes(r)

    def test_no_l_codes_in_errors_or_warnings(self):
        r = FindingCollector()
        inv = _inv_with_manifest({D1: ["v1/content/a\x00" + "x" * 5000]})
        inv["id"] = "http://example.org/" + ("x" * 2048)
        validate_inventory(inv, r)
        assert not [e.code for e in (*r.errors, *r.warnings) if e.code.startswith("L")]


class TestUnicodeNormalizationCharacterization:
    def test_nfc_and_nfd_treated_as_distinct_paths(self):
        nfc = unicodedata.normalize("NFC", "café.txt")
        nfd = unicodedata.normalize("NFD", "café.txt")
        assert nfc != nfd
        r = FindingCollector()
        manifest = {
            D1: [f"v1/content/{nfc}"],
            D2: [f"v1/content/{nfd}"],
        }
        versions = {
            "v1": {
                "created": "2024-01-01T00:00:00Z",
                "state": {D1: [nfc], D2: [nfd]},
            },
        }
        validate_inventory(_inv_with_manifest(manifest, versions=versions), r)
        assert not r.has_code("E101a")
        assert not r.has_code("E095a")
