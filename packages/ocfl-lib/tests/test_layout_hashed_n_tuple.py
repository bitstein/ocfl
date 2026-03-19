"""Tests for 0004-hashed-n-tuple-storage-layout."""

import pytest

from ocfl_lib.layout import Layout
from ocfl_lib.layout.hashed_n_tuple import HashedNTupleConfig, HashedNTupleLayout


class TestHashedNTupleLayout:
    def test_satisfies_protocol(self):
        layout = HashedNTupleLayout()
        assert isinstance(layout, Layout)

    def test_name(self):
        assert HashedNTupleLayout().name == "0004-hashed-n-tuple-storage-layout"

    # -- Example 1: sha256, tupleSize=3, numberOfTuples=3, shortObjectRoot=false --

    def test_ex1_object_01(self):
        layout = HashedNTupleLayout()
        assert layout.resolve("object-01") == (
            "3c0/ff4/240/"
            "3c0ff4240c1e116dba14c7627f2319b58aa3d77606d0d90dfc6161608ac987d4"
        )

    def test_ex1_special_chars(self):
        layout = HashedNTupleLayout()
        assert layout.resolve("..hor/rib:le-$id") == (
            "487/326/d8c/"
            "487326d8c2a3c0b885e23da1469b4d6671fd4e76978924b4443e9e3c316cda6d"
        )

    # -- Example 2: md5, tupleSize=2, numberOfTuples=15, shortObjectRoot=true --

    def test_ex2_object_01(self):
        layout = HashedNTupleLayout(
            digest_algorithm="md5",
            tuple_size=2,
            number_of_tuples=15,
            short_object_root=True,
        )
        assert layout.resolve("object-01") == (
            "ff/75/53/44/92/48/5e/ab/b3/9f/86/35/67/28/88/4e"
        )

    def test_ex2_special_chars(self):
        layout = HashedNTupleLayout(
            digest_algorithm="md5",
            tuple_size=2,
            number_of_tuples=15,
            short_object_root=True,
        )
        assert layout.resolve("..hor/rib:le-$id") == (
            "08/31/97/66/fb/6c/29/35/dd/17/5b/94/26/77/17/e0"
        )

    # -- Example 3: sha256, tupleSize=0, numberOfTuples=0, shortObjectRoot=false --

    def test_ex3_object_01(self):
        layout = HashedNTupleLayout(tuple_size=0, number_of_tuples=0)
        assert layout.resolve("object-01") == (
            "3c0ff4240c1e116dba14c7627f2319b58aa3d77606d0d90dfc6161608ac987d4"
        )

    def test_ex3_special_chars(self):
        layout = HashedNTupleLayout(tuple_size=0, number_of_tuples=0)
        assert layout.resolve("..hor/rib:le-$id") == (
            "487326d8c2a3c0b885e23da1469b4d6671fd4e76978924b4443e9e3c316cda6d"
        )

    # -- config --

    def test_config_defaults(self):
        import msgspec

        cfg = HashedNTupleConfig()
        data = msgspec.json.encode(cfg)
        decoded = msgspec.json.decode(data)
        assert decoded["extensionName"] == "0004-hashed-n-tuple-storage-layout"
        assert decoded["digestAlgorithm"] == "sha256"
        assert decoded["tupleSize"] == 3
        assert decoded["numberOfTuples"] == 3
        assert decoded["shortObjectRoot"] is False

    def test_from_config_none(self):
        layout = HashedNTupleLayout.from_config(None)
        assert layout.resolve("object-01").startswith("3c0/ff4/240/")

    def test_from_config_dict(self):
        layout = HashedNTupleLayout.from_config(
            {
                "extensionName": "0004-hashed-n-tuple-storage-layout",
                "digestAlgorithm": "md5",
                "tupleSize": 2,
                "numberOfTuples": 15,
                "shortObjectRoot": True,
            }
        )
        assert layout.resolve("object-01") == (
            "ff/75/53/44/92/48/5e/ab/b3/9f/86/35/67/28/88/4e"
        )

    # -- validation --

    def test_tuple_size_zero_requires_number_zero(self):
        with pytest.raises(ValueError):
            HashedNTupleLayout(tuple_size=0, number_of_tuples=3)

    def test_number_zero_requires_tuple_size_zero(self):
        with pytest.raises(ValueError):
            HashedNTupleLayout(tuple_size=3, number_of_tuples=0)

    def test_tuples_exceed_digest_length(self):
        # md5 hex = 32 chars, 3*11 = 33 > 32
        with pytest.raises(ValueError):
            HashedNTupleLayout(
                digest_algorithm="md5", tuple_size=3, number_of_tuples=11
            )

    def test_short_root_invalid_when_tuples_cover_full_digest(self):
        # md5 hex = 32 chars, 2*16 = 32 = full digest, short_object_root invalid
        with pytest.raises(ValueError):
            HashedNTupleLayout(
                digest_algorithm="md5",
                tuple_size=2,
                number_of_tuples=16,
                short_object_root=True,
            )
