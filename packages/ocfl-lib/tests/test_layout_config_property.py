"""Tests for Layout.config property."""

from ocfl_lib._layout import (
    FlatDirectLayout,
    FlatOmitPrefixLayout,
    HashAndIdNTupleLayout,
    HashedNTupleLayout,
    NTupleOmitPrefixLayout,
)


class TestFlatDirectConfig:
    def test_config_returns_none(self):
        """Flat direct has no meaningful config — returns None."""
        layout = FlatDirectLayout()
        assert layout.config is None


class TestHashedNTupleConfig:
    def test_default_config_round_trips(self):
        layout = HashedNTupleLayout()
        cfg = layout.config
        assert cfg is not None
        assert cfg["digestAlgorithm"] == "sha256"
        assert cfg["tupleSize"] == 3
        assert cfg["numberOfTuples"] == 3
        assert cfg["shortObjectRoot"] is False
        # Round-trip: from_config(config) produces equivalent layout
        layout2 = HashedNTupleLayout.from_config(cfg)
        assert layout2.config == cfg

    def test_custom_config_round_trips(self):
        layout = HashedNTupleLayout(
            digest_algorithm="md5",
            tuple_size=2,
            number_of_tuples=2,
            short_object_root=True,
        )
        cfg = layout.config
        assert cfg is not None
        assert cfg["digestAlgorithm"] == "md5"
        assert cfg["tupleSize"] == 2
        layout2 = HashedNTupleLayout.from_config(cfg)
        assert layout2.config == cfg

    def test_config_has_extension_name(self):
        layout = HashedNTupleLayout()
        cfg = layout.config
        assert cfg["extensionName"] == "0004-hashed-n-tuple-storage-layout"


class TestHashAndIdNTupleConfig:
    def test_default_config_round_trips(self):
        layout = HashAndIdNTupleLayout()
        cfg = layout.config
        assert cfg is not None
        assert cfg["digestAlgorithm"] == "sha256"
        assert cfg["tupleSize"] == 3
        assert cfg["numberOfTuples"] == 3
        layout2 = HashAndIdNTupleLayout.from_config(cfg)
        assert layout2.config == cfg

    def test_custom_config_round_trips(self):
        layout = HashAndIdNTupleLayout(
            digest_algorithm="md5", tuple_size=2, number_of_tuples=4
        )
        cfg = layout.config
        assert cfg is not None
        assert cfg["digestAlgorithm"] == "md5"
        assert cfg["tupleSize"] == 2
        assert cfg["numberOfTuples"] == 4
        layout2 = HashAndIdNTupleLayout.from_config(cfg)
        assert layout2.config == cfg

    def test_config_has_extension_name(self):
        layout = HashAndIdNTupleLayout()
        assert (
            layout.config["extensionName"] == "0003-hash-and-id-n-tuple-storage-layout"
        )


class TestFlatOmitPrefixConfig:
    def test_config_round_trips(self):
        layout = FlatOmitPrefixLayout(delimiter=":")
        cfg = layout.config
        assert cfg is not None
        assert cfg["delimiter"] == ":"
        layout2 = FlatOmitPrefixLayout.from_config(cfg)
        assert layout2.config == cfg

    def test_config_has_extension_name(self):
        layout = FlatOmitPrefixLayout(delimiter="/")
        assert layout.config["extensionName"] == "0006-flat-omit-prefix-storage-layout"


class TestNTupleOmitPrefixConfig:
    def test_default_config_round_trips(self):
        layout = NTupleOmitPrefixLayout()
        cfg = layout.config
        assert cfg is not None
        assert cfg["delimiter"] == ":"
        assert cfg["tupleSize"] == 3
        assert cfg["numberOfTuples"] == 3
        assert cfg["zeroPadding"] == "left"
        assert cfg["reverseObjectRoot"] is False
        layout2 = NTupleOmitPrefixLayout.from_config(cfg)
        assert layout2.config == cfg

    def test_custom_config_round_trips(self):
        layout = NTupleOmitPrefixLayout(
            delimiter="/",
            tuple_size=2,
            number_of_tuples=4,
            zero_padding="right",
            reverse_object_root=True,
        )
        cfg = layout.config
        assert cfg is not None
        assert cfg["delimiter"] == "/"
        assert cfg["tupleSize"] == 2
        assert cfg["numberOfTuples"] == 4
        assert cfg["zeroPadding"] == "right"
        assert cfg["reverseObjectRoot"] is True
        layout2 = NTupleOmitPrefixLayout.from_config(cfg)
        assert layout2.config == cfg

    def test_config_has_extension_name(self):
        layout = NTupleOmitPrefixLayout()
        assert (
            layout.config["extensionName"] == "0007-n-tuple-omit-prefix-storage-layout"
        )
