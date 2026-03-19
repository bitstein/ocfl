"""0006-flat-omit-prefix-storage-layout extension."""

import msgspec

EXTENSION_NAME = "0006-flat-omit-prefix-storage-layout"


class FlatOmitPrefixConfig(msgspec.Struct, rename="camel"):
    delimiter: str
    extension_name: str = EXTENSION_NAME


class FlatOmitPrefixLayout:
    """OCFL 0006: flat omit prefix storage layout."""

    def __init__(self, *, delimiter: str) -> None:
        if not delimiter:
            raise ValueError("delimiter must not be empty")
        self._delimiter = delimiter

    @property
    def name(self) -> str:
        return EXTENSION_NAME

    @classmethod
    def from_config(cls, config: dict | None = None) -> FlatOmitPrefixLayout:
        if config is None:
            raise ValueError("config with delimiter is required")
        cfg = msgspec.convert(config, FlatOmitPrefixConfig)
        return cls(delimiter=cfg.delimiter)

    def resolve(self, object_id: str) -> str:
        idx = object_id.rfind(self._delimiter)
        if idx < 0:
            raise ValueError(
                f"delimiter {self._delimiter!r} not found in object ID {object_id!r}"
            )
        return object_id[idx + len(self._delimiter) :]
