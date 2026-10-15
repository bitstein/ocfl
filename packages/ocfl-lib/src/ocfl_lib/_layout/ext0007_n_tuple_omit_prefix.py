from typing import Literal

import msgspec

from ocfl_lib._errors import InvalidLayoutConfig
from ocfl_lib._layout._casefold import rfind_casefold_span
from ocfl_lib._layout._component import require_disk_safe_component
from ocfl_lib._layout.types import Layout

EXTENSION_NAME = "0007-n-tuple-omit-prefix-storage-layout"


class NTupleOmitPrefixConfig(msgspec.Struct, rename="camel"):
    extension_name: str = EXTENSION_NAME
    delimiter: str = ":"
    tuple_size: int = 3
    number_of_tuples: int = 3
    zero_padding: Literal["left", "right"] = "left"
    reverse_object_root: bool = False


class NTupleOmitPrefixLayout(Layout):
    """OCFL 0007: n-tuple omit prefix storage layout."""

    def __init__(
        self,
        *,
        delimiter: str = ":",
        tuple_size: int = 3,
        number_of_tuples: int = 3,
        zero_padding: Literal["left", "right"] = "left",
        reverse_object_root: bool = False,
    ) -> None:
        if not delimiter:
            raise InvalidLayoutConfig("delimiter must not be empty")
        if tuple_size < 1:
            raise InvalidLayoutConfig(f"tupleSize must be >= 1, got {tuple_size}")
        if number_of_tuples < 1:
            raise InvalidLayoutConfig(
                f"numberOfTuples must be >= 1, got {number_of_tuples}"
            )
        self._delimiter = delimiter
        self._tuple_size = tuple_size
        self._n_tuples = number_of_tuples
        self._zero_padding = zero_padding
        self._reverse = reverse_object_root

    @property
    def name(self) -> str:
        return EXTENSION_NAME

    @property
    def config(self) -> dict:
        cfg = NTupleOmitPrefixConfig(
            delimiter=self._delimiter,
            tuple_size=self._tuple_size,
            number_of_tuples=self._n_tuples,
            zero_padding=self._zero_padding,
            reverse_object_root=self._reverse,
        )
        return msgspec.to_builtins(cfg)

    @classmethod
    def from_config(cls, config: dict | None = None) -> NTupleOmitPrefixLayout:
        if config is None:
            return cls()
        cfg = msgspec.convert(config, NTupleOmitPrefixConfig)
        return cls(
            delimiter=cfg.delimiter,
            tuple_size=cfg.tuple_size,
            number_of_tuples=cfg.number_of_tuples,
            zero_padding=cfg.zero_padding,
            reverse_object_root=cfg.reverse_object_root,
        )

    def resolve(self, object_id: str) -> str:
        match = rfind_casefold_span(object_id, self._delimiter)
        remainder = object_id[match[1] :] if match is not None else object_id
        require_disk_safe_component(remainder)

        total_len = self._tuple_size * self._n_tuples

        if self._zero_padding == "left":
            padded = remainder.rjust(total_len, "0")
        else:
            padded = remainder.ljust(total_len, "0")

        if self._reverse:
            padded = padded[::-1]

        tuples = [
            padded[i : i + self._tuple_size]
            for i in range(0, total_len, self._tuple_size)
        ]

        return "/".join([*tuples, remainder])
