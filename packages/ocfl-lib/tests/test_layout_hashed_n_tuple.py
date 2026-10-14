"""Tests for 0004-hashed-n-tuple-storage-layout."""

import msgspec
import pytest

from ocfl_lib._digest import DigestAlgorithm
from ocfl_lib._layout.ext0004_hashed_n_tuple import (
    HashedNTupleConfig,
    HashedNTupleLayout,
)

_MD5_HEX_LENGTH = DigestAlgorithm.MD5.hex_length


class TestExample1Sha256Tuple3x3:
    @pytest.fixture
    def layout(self):
        return HashedNTupleLayout()

    def test_object_01(self, layout):
        assert layout.resolve("object-01") == (
            "3c0/ff4/240/"
            "3c0ff4240c1e116dba14c7627f2319b58aa3d77606d0d90dfc6161608ac987d4"
        )

    def test_special_chars(self, layout):
        assert layout.resolve("..hor/rib:le-$id") == (
            "487/326/d8c/"
            "487326d8c2a3c0b885e23da1469b4d6671fd4e76978924b4443e9e3c316cda6d"
        )


class TestExample2Md5Tuple2x15ShortObjectRoot:
    @pytest.fixture
    def layout(self):
        return HashedNTupleLayout(
            digest_algorithm="md5",
            tuple_size=2,
            number_of_tuples=15,
            short_object_root=True,
        )

    def test_object_01(self, layout):
        assert layout.resolve("object-01") == (
            "ff/75/53/44/92/48/5e/ab/b3/9f/86/35/67/28/88/4e"
        )

    def test_special_chars(self, layout):
        assert layout.resolve("..hor/rib:le-$id") == (
            "08/31/97/66/fb/6c/29/35/dd/17/5b/94/26/77/17/e0"
        )


class TestExample3Sha256NoTuples:
    @pytest.fixture
    def layout(self):
        return HashedNTupleLayout(tuple_size=0, number_of_tuples=0)

    def test_object_01(self, layout):
        assert layout.resolve("object-01") == (
            "3c0ff4240c1e116dba14c7627f2319b58aa3d77606d0d90dfc6161608ac987d4"
        )

    def test_special_chars(self, layout):
        assert layout.resolve("..hor/rib:le-$id") == (
            "487326d8c2a3c0b885e23da1469b4d6671fd4e76978924b4443e9e3c316cda6d"
        )


class TestHashedNTupleLayout:
    def test_name(self):
        assert HashedNTupleLayout().name == "0004-hashed-n-tuple-storage-layout"

    def test_config_defaults(self):
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

    def test_tuple_size_zero_requires_number_zero(self):
        with pytest.raises(ValueError):
            HashedNTupleLayout(tuple_size=0, number_of_tuples=3)

    def test_number_zero_requires_tuple_size_zero(self):
        with pytest.raises(ValueError):
            HashedNTupleLayout(tuple_size=3, number_of_tuples=0)

    def test_tuples_exceed_digest_length(self):
        with pytest.raises(ValueError):
            HashedNTupleLayout(
                digest_algorithm="md5",
                tuple_size=3,
                number_of_tuples=_MD5_HEX_LENGTH // 3 + 1,
            )

    def test_short_root_invalid_when_tuples_cover_full_digest(self):
        with pytest.raises(ValueError):
            HashedNTupleLayout(
                digest_algorithm="md5",
                tuple_size=2,
                number_of_tuples=_MD5_HEX_LENGTH // 2,
                short_object_root=True,
            )
