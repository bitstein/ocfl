"""Tests for 0007-n-tuple-omit-prefix-storage-layout."""

import pytest

from ocfl_lib.layout import Layout
from ocfl_lib.layout.ext0007_n_tuple_omit_prefix import (
    NTupleOmitPrefixConfig,
    NTupleOmitPrefixLayout,
)


class TestNTupleOmitPrefixLayout:
    def test_satisfies_protocol(self):
        assert isinstance(NTupleOmitPrefixLayout(), Layout)

    def test_name(self):
        assert (
            NTupleOmitPrefixLayout().name == "0007-n-tuple-omit-prefix-storage-layout"
        )

    # -- Example 1: delimiter=":", tupleSize=4, numberOfTuples=2,
    #               zeroPadding="left", reverseObjectRoot=true --

    def test_ex1_simple(self):
        layout = NTupleOmitPrefixLayout(
            delimiter=":",
            tuple_size=4,
            number_of_tuples=2,
            zero_padding="left",
            reverse_object_root=True,
        )
        assert layout.resolve("namespace:12887296") == "6927/8821/12887296"

    def test_ex1_uuid(self):
        layout = NTupleOmitPrefixLayout(
            delimiter=":",
            tuple_size=4,
            number_of_tuples=2,
            zero_padding="left",
            reverse_object_root=True,
        )
        assert layout.resolve("urn:uuid:6e8bc430-9c3a-11d9-9669-0800200c9a66") == (
            "66a9/c002/6e8bc430-9c3a-11d9-9669-0800200c9a66"
        )

    def test_ex1_no_delimiter(self):
        # delimiter ":" not found → use whole ID
        layout = NTupleOmitPrefixLayout(
            delimiter=":",
            tuple_size=4,
            number_of_tuples=2,
            zero_padding="left",
            reverse_object_root=True,
        )
        assert layout.resolve("abc123") == "321c/ba00/abc123"

    # -- Example 2: delimiter="edu/", tupleSize=3, numberOfTuples=3,
    #               zeroPadding="right", reverseObjectRoot=false --

    def test_ex2_simple(self):
        layout = NTupleOmitPrefixLayout(
            delimiter="edu/",
            tuple_size=3,
            number_of_tuples=3,
            zero_padding="right",
            reverse_object_root=False,
        )
        assert (
            layout.resolve("https://institution.edu/3448793") == "344/879/300/3448793"
        )

    def test_ex2_nested(self):
        layout = NTupleOmitPrefixLayout(
            delimiter="edu/",
            tuple_size=3,
            number_of_tuples=3,
            zero_padding="right",
            reverse_object_root=False,
        )
        assert layout.resolve("https://institution.edu/abc/edu/f8.05v") == (
            "f8./05v/000/f8.05v"
        )

    # -- Validation --

    def test_empty_delimiter_raises(self):
        with pytest.raises(ValueError, match="empty"):
            NTupleOmitPrefixLayout(delimiter="")

    # -- Config --

    def test_config_defaults(self):
        import msgspec

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
        assert isinstance(layout, Layout)

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
