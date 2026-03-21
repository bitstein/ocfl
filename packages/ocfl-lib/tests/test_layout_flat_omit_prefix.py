"""Tests for 0006-flat-omit-prefix-storage-layout."""

import msgspec
import pytest

from ocfl_lib.layout import Layout
from ocfl_lib.layout.ext0006_flat_omit_prefix import (
    FlatOmitPrefixConfig,
    FlatOmitPrefixLayout,
)


class TestFlatOmitPrefixLayout:
    def test_satisfies_protocol(self):
        assert isinstance(FlatOmitPrefixLayout(delimiter=":"), Layout)

    def test_name(self):
        assert (
            FlatOmitPrefixLayout(delimiter=":").name
            == "0006-flat-omit-prefix-storage-layout"
        )

    # -- Example 1: delimiter=":" --

    def test_ex1_colon_simple(self):
        layout = FlatOmitPrefixLayout(delimiter=":")
        assert layout.resolve("namespace:12887296") == "12887296"

    def test_ex1_colon_multiple(self):
        layout = FlatOmitPrefixLayout(delimiter=":")
        assert layout.resolve("urn:uuid:6e8bc430-9c3a-11d9-9669-0800200c9a66") == (
            "6e8bc430-9c3a-11d9-9669-0800200c9a66"
        )

    # -- Example 2: delimiter="edu/" --

    def test_ex2_edu_simple(self):
        layout = FlatOmitPrefixLayout(delimiter="edu/")
        assert layout.resolve("https://institution.edu/3448793") == "3448793"

    def test_ex2_edu_multiple(self):
        layout = FlatOmitPrefixLayout(delimiter="edu/")
        assert layout.resolve("https://institution.edu/abc/edu/f8.05v") == "f8.05v"

    # -- Validation --

    def test_no_delimiter_found_raises(self):
        layout = FlatOmitPrefixLayout(delimiter=":")
        with pytest.raises(ValueError, match="delimiter"):
            layout.resolve("no-colon-here")

    def test_empty_delimiter_raises(self):
        with pytest.raises(ValueError, match="empty"):
            FlatOmitPrefixLayout(delimiter="")

    # -- Config --

    def test_config_struct(self):
        cfg = FlatOmitPrefixConfig(delimiter=":")
        data = msgspec.json.decode(msgspec.json.encode(cfg))
        assert data["extensionName"] == "0006-flat-omit-prefix-storage-layout"
        assert data["delimiter"] == ":"

    def test_from_config(self):
        layout = FlatOmitPrefixLayout.from_config(
            {
                "extensionName": "0006-flat-omit-prefix-storage-layout",
                "delimiter": "edu/",
            }
        )
        assert layout.resolve("https://institution.edu/3448793") == "3448793"

    def test_from_config_missing_delimiter_raises(self):
        with pytest.raises((ValueError, TypeError, msgspec.ValidationError)):
            FlatOmitPrefixLayout.from_config(
                {
                    "extensionName": "0006-flat-omit-prefix-storage-layout",
                }
            )
