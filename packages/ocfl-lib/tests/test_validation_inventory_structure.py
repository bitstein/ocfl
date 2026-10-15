from builders import inventory
from digests import D1

from ocfl_lib.validation._inventory import validate_inventory
from ocfl_lib.validation._report import FindingCollector


class TestValidInventory:
    def test_minimal_valid_inventory(self):
        r = FindingCollector()
        inv = validate_inventory(inventory(), r, lax_digests=True)
        assert r.is_valid, [e.code for e in r.errors]
        assert inv is not None
        assert inv.id == "http://example.org/obj1"


class TestRequiredKeys:
    def test_missing_id(self):
        d = inventory()
        del d["id"]
        r = FindingCollector()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E036a")

    def test_missing_type(self):
        d = inventory()
        del d["type"]
        r = FindingCollector()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E036b")

    def test_missing_digest_algorithm(self):
        d = inventory()
        del d["digestAlgorithm"]
        r = FindingCollector()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E036c")

    def test_missing_head(self):
        d = inventory()
        del d["head"]
        r = FindingCollector()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E036d")

    def test_missing_manifest(self):
        d = inventory()
        del d["manifest"]
        r = FindingCollector()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E041a")

    def test_missing_versions(self):
        d = inventory()
        del d["versions"]
        r = FindingCollector()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E041b")


class TestUnknownKeys:
    def test_unknown_top_level_key(self):
        d = inventory()
        d["extra"] = "bad"
        r = FindingCollector()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E102")


class TestIdValidation:
    def test_empty_id(self):
        d = inventory()
        d["id"] = ""
        r = FindingCollector()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E037a")

    def test_non_string_id(self):
        d = inventory()
        d["id"] = 42
        r = FindingCollector()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E037a")

    def test_non_uri_id_warning(self):
        d = inventory()
        d["id"] = "not a uri"
        r = FindingCollector()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("W005")


class TestTypeValidation:
    def test_non_string_type(self):
        d = inventory()
        d["type"] = 42
        r = FindingCollector()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E038d")

    def test_invalid_type_url(self):
        d = inventory()
        d["type"] = "not-a-url"
        r = FindingCollector()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E038b")

    def test_unsupported_version(self):
        d = inventory()
        d["type"] = "https://ocfl.io/9.9/spec/#inventory"
        r = FindingCollector()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E038c")

    def test_valid_1_0_type(self):
        d = inventory()
        d["type"] = "https://ocfl.io/1.0/spec/#inventory"
        r = FindingCollector()
        validate_inventory(d, r, lax_digests=True)
        assert not r.has_code("E038")


class TestDigestAlgorithmValidation:
    def test_invalid_algorithm(self):
        d = inventory()
        d["digestAlgorithm"] = "md5"
        r = FindingCollector()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E025a")

    def test_sha256_warning(self):
        d = inventory()
        d["digestAlgorithm"] = "sha256"
        r = FindingCollector()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("W004")

    def test_sha512_no_warning(self):
        d = inventory()
        r = FindingCollector()
        validate_inventory(d, r, lax_digests=True)
        assert not r.has_code("W004")


class TestContentDirectory:
    def test_non_string_content_dir(self):
        d = inventory()
        d["contentDirectory"] = 42
        r = FindingCollector()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E017")

    def test_slash_in_content_dir(self):
        d = inventory()
        d["contentDirectory"] = "a/b"
        r = FindingCollector()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E017")

    def test_dot_content_dir(self):
        d = inventory()
        d["contentDirectory"] = "."
        r = FindingCollector()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E018")

    def test_dotdot_content_dir(self):
        d = inventory()
        d["contentDirectory"] = ".."
        r = FindingCollector()
        validate_inventory(d, r, lax_digests=True)
        assert r.has_code("E018")

    def test_valid_content_dir(self):
        d = inventory()
        d["contentDirectory"] = "data"
        d["manifest"] = {D1: ["v1/data/file.txt"]}
        r = FindingCollector()
        validate_inventory(d, r, lax_digests=True)
        assert not r.has_code("E017")
        assert not r.has_code("E018")
