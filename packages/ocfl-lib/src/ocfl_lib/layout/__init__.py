"""OCFL storage layout extensions."""

from ocfl_lib.layout.config import (
    read_layout_config,
    read_ocfl_layout,
    write_layout_config,
    write_ocfl_layout,
)
from ocfl_lib.layout.ext0002_flat_direct import FlatDirectLayout
from ocfl_lib.layout.ext0003_hash_and_id_n_tuple import HashAndIdNTupleLayout
from ocfl_lib.layout.ext0004_hashed_n_tuple import HashedNTupleLayout
from ocfl_lib.layout.ext0006_flat_omit_prefix import FlatOmitPrefixLayout
from ocfl_lib.layout.ext0007_n_tuple_omit_prefix import NTupleOmitPrefixLayout
from ocfl_lib.layout.registry import LayoutRegistry
from ocfl_lib.layout.types import Layout

__all__ = [
    "DEFAULT_LAYOUTS",
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

# Built-in layouts. Frozen; pass a custom registry explicitly to extend it.
DEFAULT_LAYOUTS = LayoutRegistry(
    {
        "0002-flat-direct-storage-layout": FlatDirectLayout.from_config,
        "0003-hash-and-id-n-tuple-storage-layout": HashAndIdNTupleLayout.from_config,
        "0004-hashed-n-tuple-storage-layout": HashedNTupleLayout.from_config,
        "0006-flat-omit-prefix-storage-layout": FlatOmitPrefixLayout.from_config,
        "0007-n-tuple-omit-prefix-storage-layout": NTupleOmitPrefixLayout.from_config,
    }
)


def get_layout(
    name: str,
    *,
    config: dict | None = None,
    layouts: LayoutRegistry = DEFAULT_LAYOUTS,
) -> Layout:
    """Get a layout by extension name from `layouts`."""
    return layouts.get_layout(name, config=config)
