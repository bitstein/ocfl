"""Tests for 0003-hash-and-id-n-tuple-storage-layout."""

import msgspec
import pytest

from ocfl_lib.layout import Layout
from ocfl_lib.layout.ext0003_hash_and_id_n_tuple import (
    HashAndIdNTupleConfig,
    HashAndIdNTupleLayout,
    _percent_encode,
)


class TestHashAndIdNTupleLayout:
    def test_satisfies_protocol(self):
        assert isinstance(HashAndIdNTupleLayout(), Layout)

    def test_name(self):
        assert HashAndIdNTupleLayout().name == "0003-hash-and-id-n-tuple-storage-layout"

    # -- Example 1: sha256, tupleSize=3, numberOfTuples=3 (defaults) --

    def test_ex1_object_01(self):
        layout = HashAndIdNTupleLayout()
        assert layout.resolve("object-01") == "3c0/ff4/240/object-01"

    def test_ex1_special_chars(self):
        layout = HashAndIdNTupleLayout()
        assert layout.resolve("..hor/rib:le-$id") == (
            "487/326/d8c/%2e%2ehor%2frib%3ale-%24id"
        )

    # -- Example 2: md5, tupleSize=2, numberOfTuples=15 --

    def test_ex2_object_01(self):
        layout = HashAndIdNTupleLayout(
            digest_algorithm="md5", tuple_size=2, number_of_tuples=15
        )
        assert layout.resolve("object-01") == (
            "ff/75/53/44/92/48/5e/ab/b3/9f/86/35/67/28/88/object-01"
        )

    def test_ex2_special_chars(self):
        layout = HashAndIdNTupleLayout(
            digest_algorithm="md5", tuple_size=2, number_of_tuples=15
        )
        assert layout.resolve("..hor/rib:le-$id") == (
            "08/31/97/66/fb/6c/29/35/dd/17/5b/94/26/77/17/%2e%2ehor%2frib%3ale-%24id"
        )

    # -- Example 3: sha256, tupleSize=0, numberOfTuples=0 --

    def test_ex3_object_01(self):
        layout = HashAndIdNTupleLayout(tuple_size=0, number_of_tuples=0)
        assert layout.resolve("object-01") == "object-01"

    def test_ex3_special_chars(self):
        layout = HashAndIdNTupleLayout(tuple_size=0, number_of_tuples=0)
        assert layout.resolve("..hor/rib:le-$id") == "%2e%2ehor%2frib%3ale-%24id"

    # -- Percent encoding --

    def test_encode_dot(self):
        assert _percent_encode(".") == "%2e"

    def test_encode_cedilla(self):
        assert _percent_encode("\u00e7") == "%c3%a7"

    def test_encode_safe_chars_unchanged(self):
        assert _percent_encode("abc-XYZ_012") == "abc-XYZ_012"

    # -- Truncation at 100 chars --

    def test_truncation_long_id(self):
        # 260-char ID → encoded exceeds 100, truncated + digest appended
        layout = HashAndIdNTupleLayout()
        long_id = "abcdefghij" * 26
        result = layout.resolve(long_id)
        parts = result.split("/")
        # 3 tuple dirs + encoded leaf
        assert len(parts) == 4
        leaf = parts[3]
        assert len(leaf.split("-")[-1]) == 64  # sha256 hex
        assert leaf.startswith("abcdefghij")

    def test_truncation_101_char_id(self):
        layout = HashAndIdNTupleLayout()
        long_id = "abcdefghij" * 10 + "a"  # 101 chars, all safe, encoded=101 > 100
        result = layout.resolve(long_id)
        parts = result.split("/")
        leaf = parts[3]
        # should be truncated: 100 chars + '-' + 64-char digest
        assert "-" in leaf
        prefix, digest = leaf.rsplit("-", 1)
        assert len(prefix) == 100
        assert len(digest) == 64

    def test_no_truncation_100_char_id(self):
        layout = HashAndIdNTupleLayout()
        id_100 = "abcdefghij" * 10  # exactly 100 safe chars
        result = layout.resolve(id_100)
        parts = result.split("/")
        leaf = parts[3]
        # 100 chars, no truncation needed
        assert leaf == id_100

    # -- Config --

    def test_config_defaults(self):
        cfg = HashAndIdNTupleConfig()
        data = msgspec.json.decode(msgspec.json.encode(cfg))
        assert data["extensionName"] == "0003-hash-and-id-n-tuple-storage-layout"
        assert data["digestAlgorithm"] == "sha256"
        assert data["tupleSize"] == 3
        assert data["numberOfTuples"] == 3

    def test_from_config_none(self):
        layout = HashAndIdNTupleLayout.from_config(None)
        assert layout.resolve("object-01") == "3c0/ff4/240/object-01"

    def test_from_config_dict(self):
        layout = HashAndIdNTupleLayout.from_config(
            {
                "extensionName": "0003-hash-and-id-n-tuple-storage-layout",
                "digestAlgorithm": "md5",
                "tupleSize": 2,
                "numberOfTuples": 15,
            }
        )
        assert layout.resolve("object-01") == (
            "ff/75/53/44/92/48/5e/ab/b3/9f/86/35/67/28/88/object-01"
        )

    # -- Validation --

    def test_tuple_size_zero_requires_number_zero(self):
        with pytest.raises(ValueError):
            HashAndIdNTupleLayout(tuple_size=0, number_of_tuples=3)
