import msgspec
import pytest

from ocfl_lib._digest import DigestAlgorithm
from ocfl_lib._layout.ext0003_hash_and_id_n_tuple import (
    _MAX_ENCODED_LENGTH,
    HashAndIdNTupleConfig,
    HashAndIdNTupleLayout,
    _percent_encode,
)

_SHA256_HEX_LENGTH = DigestAlgorithm.SHA256.hex_length


class TestExample1Sha256Tuple3x3Defaults:
    @pytest.fixture
    def layout(self):
        return HashAndIdNTupleLayout()

    def test_object_01(self, layout):
        assert layout.resolve("object-01") == "3c0/ff4/240/object-01"

    def test_special_chars(self, layout):
        assert layout.resolve("..hor/rib:le-$id") == (
            "487/326/d8c/%2e%2ehor%2frib%3ale-%24id"
        )


class TestExample2Md5Tuple2x15:
    @pytest.fixture
    def layout(self):
        return HashAndIdNTupleLayout(
            digest_algorithm="md5", tuple_size=2, number_of_tuples=15
        )

    def test_object_01(self, layout):
        assert layout.resolve("object-01") == (
            "ff/75/53/44/92/48/5e/ab/b3/9f/86/35/67/28/88/object-01"
        )

    def test_special_chars(self, layout):
        assert layout.resolve("..hor/rib:le-$id") == (
            "08/31/97/66/fb/6c/29/35/dd/17/5b/94/26/77/17/%2e%2ehor%2frib%3ale-%24id"
        )


class TestExample3Sha256NoTuples:
    @pytest.fixture
    def layout(self):
        return HashAndIdNTupleLayout(tuple_size=0, number_of_tuples=0)

    def test_object_01(self, layout):
        assert layout.resolve("object-01") == "object-01"

    def test_special_chars(self, layout):
        assert layout.resolve("..hor/rib:le-$id") == "%2e%2ehor%2frib%3ale-%24id"


class TestHashAndIdNTupleLayout:
    def test_name(self):
        assert HashAndIdNTupleLayout().name == "0003-hash-and-id-n-tuple-storage-layout"

    def test_encode_dot(self):
        assert _percent_encode(".") == "%2e"

    def test_encode_cedilla(self):
        assert _percent_encode("\u00e7") == "%c3%a7"

    def test_encode_safe_chars_unchanged(self):
        assert _percent_encode("abc-XYZ_012") == "abc-XYZ_012"

    def test_truncation_long_id(self):
        long_id = "abcdefghij" * 26
        assert len(_percent_encode(long_id)) > _MAX_ENCODED_LENGTH
        *tuple_dirs, leaf = HashAndIdNTupleLayout().resolve(long_id).split("/")
        assert len(tuple_dirs) == 3
        assert len(leaf.split("-")[-1]) == _SHA256_HEX_LENGTH
        assert leaf.startswith("abcdefghij")

    def test_truncation_just_over_max_length(self):
        long_id = "abcdefghij" * 10 + "a"
        assert len(long_id) == _MAX_ENCODED_LENGTH + 1
        leaf = HashAndIdNTupleLayout().resolve(long_id).split("/")[3]
        assert "-" in leaf
        prefix, digest = leaf.rsplit("-", 1)
        assert len(prefix) == _MAX_ENCODED_LENGTH
        assert len(digest) == _SHA256_HEX_LENGTH

    def test_no_truncation_at_max_length(self):
        id_at_max = "abcdefghij" * 10
        assert len(id_at_max) == _MAX_ENCODED_LENGTH
        leaf = HashAndIdNTupleLayout().resolve(id_at_max).split("/")[3]
        assert leaf == id_at_max

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

    def test_tuple_size_zero_requires_number_zero(self):
        with pytest.raises(ValueError):
            HashAndIdNTupleLayout(tuple_size=0, number_of_tuples=3)
