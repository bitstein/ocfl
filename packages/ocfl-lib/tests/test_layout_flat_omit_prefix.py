import msgspec
import pytest

from ocfl_lib._layout.ext0006_flat_omit_prefix import (
    FlatOmitPrefixConfig,
    FlatOmitPrefixLayout,
)


class TestExample1ColonDelimiter:
    @pytest.fixture
    def layout(self):
        return FlatOmitPrefixLayout(delimiter=":")

    def test_simple(self, layout):
        assert layout.resolve("namespace:12887296") == "12887296"

    def test_multiple(self, layout):
        assert layout.resolve("urn:uuid:6e8bc430-9c3a-11d9-9669-0800200c9a66") == (
            "6e8bc430-9c3a-11d9-9669-0800200c9a66"
        )

    def test_no_delimiter_found_raises(self, layout):
        with pytest.raises(ValueError, match="delimiter"):
            layout.resolve("no-colon-here")


class TestExample2EduSlashDelimiter:
    @pytest.fixture
    def layout(self):
        return FlatOmitPrefixLayout(delimiter="edu/")

    def test_simple(self, layout):
        assert layout.resolve("https://institution.edu/3448793") == "3448793"

    def test_multiple(self, layout):
        assert layout.resolve("https://institution.edu/abc/edu/f8.05v") == "f8.05v"

    def test_delimiter_is_case_insensitive(self, layout):
        assert layout.resolve("https://institution.EDU/3448793") == "3448793"

    def test_case_insensitive_rightmost_delimiter_wins(self, layout):
        assert layout.resolve("https://x.EDU/abc/edu/f8.05v") == "f8.05v"


class TestFlatOmitPrefixLayout:
    def test_name(self):
        assert (
            FlatOmitPrefixLayout(delimiter=":").name
            == "0006-flat-omit-prefix-storage-layout"
        )

    def test_casefold_expansion_keeps_original_slice_boundary(self):
        layout = FlatOmitPrefixLayout(delimiter="ss/")
        assert layout.resolve("prefix-ß/value") == "value"

    def test_empty_delimiter_raises(self):
        with pytest.raises(ValueError, match="empty"):
            FlatOmitPrefixLayout(delimiter="")

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
