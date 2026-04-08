"""Tests for 0002-flat-direct-storage-layout."""

import msgspec
import pytest

from ocfl_lib.layout import Layout
from ocfl_lib.layout.ext0002_flat_direct import FlatDirectConfig, FlatDirectLayout


class TestFlatDirectLayout:
    def test_satisfies_protocol(self):
        layout = FlatDirectLayout()
        assert isinstance(layout, Layout)

    def test_name(self):
        assert FlatDirectLayout().name == "0002-flat-direct-storage-layout"

    # -- spec test vectors --

    def test_resolve_simple_id(self):
        assert FlatDirectLayout().resolve("object-01") == "object-01"

    def test_resolve_special_chars(self):
        assert FlatDirectLayout().resolve("..hor_rib:le-$id") == "..hor_rib:le-$id"

    # -- validation --

    def test_reject_id_with_slash(self):
        with pytest.raises(ValueError, match="separator"):
            FlatDirectLayout().resolve("info:fedora/object-01")

    def test_reject_empty_id(self):
        with pytest.raises(ValueError, match="empty"):
            FlatDirectLayout().resolve("")

    def test_reject_dot(self):
        with pytest.raises(ValueError, match=r"'\.'"):
            FlatDirectLayout().resolve(".")

    def test_reject_dotdot(self):
        with pytest.raises(ValueError, match=r"'\.\.'"):
            FlatDirectLayout().resolve("..")

    # -- config --

    def test_config_struct(self):
        cfg = FlatDirectConfig()
        data = msgspec.json.encode(cfg)
        decoded = msgspec.json.decode(data, type=FlatDirectConfig)
        assert decoded.extension_name == "0002-flat-direct-storage-layout"

    def test_from_config_none(self):
        layout = FlatDirectLayout.from_config(None)
        assert layout.name == "0002-flat-direct-storage-layout"

    def test_from_config_dict(self):
        layout = FlatDirectLayout.from_config(
            {"extensionName": "0002-flat-direct-storage-layout"}
        )
        assert layout.name == "0002-flat-direct-storage-layout"
