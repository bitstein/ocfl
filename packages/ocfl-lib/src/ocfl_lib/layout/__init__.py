"""OCFL storage layout extensions."""

from ocfl_lib.layout.config import (
    read_layout_config,
    read_ocfl_layout,
    write_layout_config,
    write_ocfl_layout,
)
from ocfl_lib.layout.flat_direct import FlatDirectLayout
from ocfl_lib.layout.flat_omit_prefix import FlatOmitPrefixLayout
from ocfl_lib.layout.hash_and_id_n_tuple import HashAndIdNTupleLayout
from ocfl_lib.layout.hashed_n_tuple import HashedNTupleLayout
from ocfl_lib.layout.n_tuple_omit_prefix import NTupleOmitPrefixLayout
from ocfl_lib.layout.registry import LayoutRegistry
from ocfl_lib.layout.types import Layout

__all__ = [
    "FlatDirectLayout",
    "FlatOmitPrefixLayout",
    "HashAndIdNTupleLayout",
    "HashedNTupleLayout",
    "Layout",
    "LayoutRegistry",
    "NTupleOmitPrefixLayout",
    "read_layout_config",
    "read_ocfl_layout",
    "write_layout_config",
    "write_ocfl_layout",
]

# Default registry with all built-in layouts
_default_registry = LayoutRegistry()
_default_registry.register(
    "0002-flat-direct-storage-layout", FlatDirectLayout.from_config
)
_default_registry.register(
    "0003-hash-and-id-n-tuple-storage-layout", HashAndIdNTupleLayout.from_config
)
_default_registry.register(
    "0004-hashed-n-tuple-storage-layout", HashedNTupleLayout.from_config
)
_default_registry.register(
    "0006-flat-omit-prefix-storage-layout", FlatOmitPrefixLayout.from_config
)
_default_registry.register(
    "0007-n-tuple-omit-prefix-storage-layout", NTupleOmitPrefixLayout.from_config
)


def get_layout(name: str, *, config: dict | None = None) -> Layout:
    """Get a layout from the default registry."""
    return _default_registry.get_layout(name, config=config)
