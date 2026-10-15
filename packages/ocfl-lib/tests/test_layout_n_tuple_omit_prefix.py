import msgspec
import pytest

from ocfl_lib._layout.ext0007_n_tuple_omit_prefix import (
    NTupleOmitPrefixConfig,
    NTupleOmitPrefixLayout,
)


class TestExample1ColonTuple4x2LeftPadReversed:
    @pytest.fixture
    def layout(self):
        return NTupleOmitPrefixLayout(
            delimiter=":",
            tuple_size=4,
            number_of_tuples=2,
            zero_padding="left",
            reverse_object_root=True,
        )

    def test_simple(self, layout):
        assert layout.resolve("namespace:12887296") == "6927/8821/12887296"

    def test_uuid(self, layout):
        assert layout.resolve("urn:uuid:6e8bc430-9c3a-11d9-9669-0800200c9a66") == (
            "66a9/c002/6e8bc430-9c3a-11d9-9669-0800200c9a66"
        )

    def test_no_delimiter(self, layout):
        assert layout.resolve("abc123") == "321c/ba00/abc123"


class TestExample2EduSlashTuple3x3RightPad:
    @pytest.fixture
    def layout(self):
        return NTupleOmitPrefixLayout(
            delimiter="edu/",
            tuple_size=3,
            number_of_tuples=3,
            zero_padding="right",
            reverse_object_root=False,
        )

    def test_simple(self, layout):
        assert (
            layout.resolve("https://institution.edu/3448793") == "344/879/300/3448793"
        )

    def test_nested(self, layout):
        assert layout.resolve("https://institution.edu/abc/edu/f8.05v") == (
            "f8./05v/000/f8.05v"
        )

    def test_delimiter_is_case_insensitive(self, layout):
        assert (
            layout.resolve("https://institution.EDU/3448793") == "344/879/300/3448793"
        )

    def test_case_insensitive_rightmost_delimiter_wins(self, layout):
        assert layout.resolve("https://x.EDU/abc/edu/f8.05v") == "f8./05v/000/f8.05v"


class TestNTupleOmitPrefixLayout:
    def test_name(self):
        assert (
            NTupleOmitPrefixLayout().name == "0007-n-tuple-omit-prefix-storage-layout"
        )

    def test_casefold_expansion_keeps_original_slice_boundary(self):
        layout = NTupleOmitPrefixLayout(
            delimiter="ss/",
            tuple_size=2,
            number_of_tuples=2,
            zero_padding="right",
            reverse_object_root=False,
        )
        assert layout.resolve("prefix-ß/value") == "va/lu/value"

    def test_empty_delimiter_raises(self):
        with pytest.raises(ValueError, match="empty"):
            NTupleOmitPrefixLayout(delimiter="")

    def test_config_defaults(self):
        cfg = NTupleOmitPrefixConfig()
        data = msgspec.json.decode(msgspec.json.encode(cfg))
        assert data["extensionName"] == "0007-n-tuple-omit-prefix-storage-layout"
        assert data["delimiter"] == ":"
        assert data["tupleSize"] == 3
        assert data["numberOfTuples"] == 3
        assert data["zeroPadding"] == "left"
        assert data["reverseObjectRoot"] is False

    def test_from_config_none(self):
        layout = NTupleOmitPrefixLayout.from_config(None)
        assert layout is not None

    def test_from_config_dict(self):
        layout = NTupleOmitPrefixLayout.from_config(
            {
                "extensionName": "0007-n-tuple-omit-prefix-storage-layout",
                "delimiter": ":",
                "tupleSize": 4,
                "numberOfTuples": 2,
                "zeroPadding": "left",
                "reverseObjectRoot": True,
            }
        )
        assert layout.resolve("namespace:12887296") == "6927/8821/12887296"
