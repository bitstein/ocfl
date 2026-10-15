from builders import inventory

from ocfl_lib.validation._inventory import validate_inventory
from ocfl_lib.validation._report import FindingCollector


class TestFixityStructure:
    def test_fixity_not_object(self):
        r = FindingCollector()
        validate_inventory(inventory(fixity="bad"), r)
        assert r.has_code("E056a")

    def test_unknown_algorithm(self):
        r = FindingCollector()
        validate_inventory(inventory(fixity={"unknown-alg": {}}), r)
        assert r.has_code("E056b")

    def test_known_algorithm_accepted(self):
        r = FindingCollector()
        validate_inventory(
            inventory(fixity={"md5": {"d" * 32: ["v1/content/file.txt"]}}), r
        )
        assert not r.has_code("E056b")

    def test_lax_digests_accepts_unknown(self):
        r = FindingCollector()
        validate_inventory(
            inventory(fixity={"unknown-alg": {}}),
            r,
            lax_digests=True,
        )
        assert not r.has_code("E056b")


class TestFixityDigestMap:
    def test_value_not_object(self):
        r = FindingCollector()
        validate_inventory(inventory(fixity={"md5": "bad"}), r)
        assert r.has_code("E057a")

    def test_bad_digest_format(self):
        r = FindingCollector()
        validate_inventory(
            inventory(fixity={"md5": {"not-hex": ["v1/content/file.txt"]}}), r
        )
        assert r.has_code("E057b")

    def test_value_not_list(self):
        r = FindingCollector()
        validate_inventory(
            inventory(fixity={"md5": {"d" * 32: "v1/content/file.txt"}}), r
        )
        assert r.has_code("E057c")

    def test_path_not_in_manifest(self):
        r = FindingCollector()
        validate_inventory(
            inventory(fixity={"md5": {"d" * 32: ["v1/content/nonexistent.txt"]}}),
            r,
        )
        assert r.has_code("E057d")

    def test_path_with_invalid_segment(self):
        r = FindingCollector()
        validate_inventory(
            inventory(fixity={"md5": {"d" * 32: ["v1/content/../file.txt"]}}),
            r,
        )
        assert r.has_code("E099")
        assert not r.has_code("E057d")

    def test_path_with_leading_slash(self):
        r = FindingCollector()
        validate_inventory(
            inventory(fixity={"md5": {"d" * 32: ["/v1/content/file.txt"]}}),
            r,
        )
        assert r.has_code("E100")
        assert not r.has_code("E057d")

    def test_path_in_manifest_accepted(self):
        r = FindingCollector()
        validate_inventory(
            inventory(fixity={"md5": {"d" * 32: ["v1/content/file.txt"]}}), r
        )
        assert not r.has_code("E057d")


class TestFixityDuplicateDigests:
    def test_case_insensitive_duplicate(self):
        r = FindingCollector()
        validate_inventory(
            inventory(
                fixity={
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
        r = FindingCollector()
        validate_inventory(
            inventory(
                fixity={
                    "sha256": {"e" * 64: ["v1/content/file.txt"]},
                    "md5": {"d" * 32: ["v1/content/file.txt"]},
                }
            ),
            r,
        )
        assert r.is_valid
