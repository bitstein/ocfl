"""0002-flat-direct-storage-layout extension."""

import msgspec

EXTENSION_NAME = "0002-flat-direct-storage-layout"


class FlatDirectConfig(msgspec.Struct, rename="camel"):
    extension_name: str = EXTENSION_NAME


class FlatDirectLayout:
    """OCFL 0002: flat direct storage layout (identity mapping)."""

    @property
    def name(self) -> str:
        return EXTENSION_NAME

    @classmethod
    def from_config(cls, config: dict | None = None) -> FlatDirectLayout:
        if config is not None:
            msgspec.convert(config, FlatDirectConfig)
        return cls()

    def resolve(self, object_id: str) -> str:
        if not object_id:
            raise ValueError("object ID must not be empty")
        if "/" in object_id:
            raise ValueError("object ID must not contain a slash")
        if object_id == ".":
            raise ValueError("object ID must not be '.'")
        if object_id == "..":
            raise ValueError("object ID must not be '..'")
        return object_id
