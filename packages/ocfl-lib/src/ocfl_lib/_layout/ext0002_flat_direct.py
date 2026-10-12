"""0002-flat-direct-storage-layout extension."""

import msgspec

from ocfl_lib._layout.types import Layout
from ocfl_lib.validation._limits import validate_path_component

EXTENSION_NAME = "0002-flat-direct-storage-layout"


class FlatDirectConfig(msgspec.Struct, rename="camel"):
    extension_name: str = EXTENSION_NAME


class FlatDirectLayout(Layout):
    """OCFL 0002: flat direct storage layout (identity mapping)."""

    @property
    def name(self) -> str:
        return EXTENSION_NAME

    @property
    def config(self) -> dict | None:
        return None

    @classmethod
    def from_config(cls, config: dict | None = None) -> FlatDirectLayout:
        if config is not None:
            msgspec.convert(config, FlatDirectConfig)
        return cls()

    def resolve(self, object_id: str) -> str:
        validate_path_component(object_id)
        return object_id
