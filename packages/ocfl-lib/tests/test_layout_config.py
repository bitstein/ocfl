"""Tests for layout config I/O."""

import json

import pytest

from ocfl_lib._layout.config import (
    read_layout_config,
    read_ocfl_layout,
    write_layout_config,
    write_ocfl_layout,
)
from ocfl_lib.fs import LocalFS


@pytest.fixture
def fs(tmp_path):
    return LocalFS(tmp_path)


class TestLayoutConfigIO:
    def test_write_and_read_layout_config(self, fs, tmp_path):
        config = {"extensionName": "0004-hashed-n-tuple-storage-layout", "tupleSize": 3}
        write_layout_config(fs, "0004-hashed-n-tuple-storage-layout", config)
        result = read_layout_config(fs, "0004-hashed-n-tuple-storage-layout")
        assert result == config

    def test_read_layout_config_not_found(self, fs):
        assert read_layout_config(fs, "nonexistent-layout") is None

    def test_write_creates_extension_dir(self, fs, tmp_path):
        config = {"extensionName": "0002-flat-direct-storage-layout"}
        write_layout_config(fs, "0002-flat-direct-storage-layout", config)
        assert (
            tmp_path / "extensions" / "0002-flat-direct-storage-layout" / "config.json"
        ).exists()

    def test_write_and_read_ocfl_layout(self, fs):
        write_ocfl_layout(
            fs,
            extension="0004-hashed-n-tuple-storage-layout",
            description="Hashed n-tuple",
        )
        result = read_ocfl_layout(fs)
        assert result is not None
        ext, desc = result
        assert ext == "0004-hashed-n-tuple-storage-layout"
        assert desc == "Hashed n-tuple"

    def test_read_ocfl_layout_not_found(self, fs):
        assert read_ocfl_layout(fs) is None

    def test_ocfl_layout_json_structure(self, fs, tmp_path):
        write_ocfl_layout(
            fs, extension="0002-flat-direct-storage-layout", description="Flat direct"
        )
        data = json.loads((tmp_path / "ocfl_layout.json").read_text())
        assert data["extension"] == "0002-flat-direct-storage-layout"
        assert data["description"] == "Flat direct"
