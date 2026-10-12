"""0006-flat-omit-prefix-storage-layout extension."""

import msgspec

from ocfl_lib.errors import InvalidLayoutConfig
from ocfl_lib.layout._casefold import rfind_casefold_span
from ocfl_lib.layout.types import Layout
from ocfl_lib.validation.limits import validate_path_component

EXTENSION_NAME = "0006-flat-omit-prefix-storage-layout"


class FlatOmitPrefixConfig(msgspec.Struct, rename="camel"):
    delimiter: str
    extension_name: str = EXTENSION_NAME


class FlatOmitPrefixLayout(Layout):
    """OCFL 0006: flat omit prefix storage layout."""

    def __init__(self, *, delimiter: str) -> None:
        if not delimiter:
            raise InvalidLayoutConfig("delimiter must not be empty")
        self._delimiter = delimiter

    @property
    def name(self) -> str:
        return EXTENSION_NAME

    @property
    def config(self) -> dict:
        cfg = FlatOmitPrefixConfig(delimiter=self._delimiter)
        return msgspec.to_builtins(cfg)

    @classmethod
    def from_config(cls, config: dict | None = None) -> FlatOmitPrefixLayout:
        if config is None:
            raise InvalidLayoutConfig("config with delimiter is required")
        cfg = msgspec.convert(config, FlatOmitPrefixConfig)
        return cls(delimiter=cfg.delimiter)

    def resolve(self, object_id: str) -> str:
        match = rfind_casefold_span(object_id, self._delimiter)
        if match is None:
            raise InvalidLayoutConfig(
                f"delimiter {self._delimiter!r} not found in object ID {object_id!r}"
            )
        remainder = object_id[match[1] :]
        validate_path_component(remainder)
        return remainder
