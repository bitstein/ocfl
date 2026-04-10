"""0003-hash-and-id-n-tuple-storage-layout extension."""

import msgspec

from ocfl_lib.digest import DigestAlgorithm
from ocfl_lib.layout.types import Layout

EXTENSION_NAME = "0003-hash-and-id-n-tuple-storage-layout"

_SAFE = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
_MAX_ENCODED_LENGTH = 100


def _percent_encode(s: str) -> str:
    """Percent-encode a string. Safe chars: [A-Za-z0-9-_]."""
    out: list[str] = []
    for byte in s.encode("utf-8"):
        if byte in _SAFE:
            out.append(chr(byte))
        else:
            out.append(f"%{byte:02x}")
    return "".join(out)


class HashAndIdNTupleConfig(msgspec.Struct, rename="camel"):
    extension_name: str = EXTENSION_NAME
    digest_algorithm: str = "sha256"
    tuple_size: int = 3
    number_of_tuples: int = 3


class HashAndIdNTupleLayout(Layout):
    """OCFL 0003: hash and id n-tuple storage layout."""

    def __init__(
        self,
        *,
        digest_algorithm: str = "sha256",
        tuple_size: int = 3,
        number_of_tuples: int = 3,
    ) -> None:
        self._alg = DigestAlgorithm.from_ocfl_name(digest_algorithm)
        self._tuple_size = tuple_size
        self._n_tuples = number_of_tuples
        self._validate()

    def _validate(self) -> None:
        ts, nt = self._tuple_size, self._n_tuples
        if (ts == 0) != (nt == 0):
            raise ValueError(
                "tupleSize and numberOfTuples must both be 0 or both non-zero"
            )
        if ts * nt > self._alg.hex_length:
            raise ValueError(
                f"tupleSize*numberOfTuples ({ts * nt}) exceeds "
                f"digest hex length ({self._alg.hex_length})"
            )

    @property
    def name(self) -> str:
        return EXTENSION_NAME

    @property
    def config(self) -> dict:
        cfg = HashAndIdNTupleConfig(
            digest_algorithm=self._alg.ocfl_name,
            tuple_size=self._tuple_size,
            number_of_tuples=self._n_tuples,
        )
        return msgspec.to_builtins(cfg)

    @classmethod
    def from_config(cls, config: dict | None = None) -> HashAndIdNTupleLayout:
        if config is None:
            return cls()
        cfg = msgspec.convert(config, HashAndIdNTupleConfig)
        return cls(
            digest_algorithm=cfg.digest_algorithm,
            tuple_size=cfg.tuple_size,
            number_of_tuples=cfg.number_of_tuples,
        )

    def resolve(self, object_id: str) -> str:
        digest = self._hash_id(object_id)
        prefix_len = self._tuple_size * self._n_tuples
        tuples = (
            [
                digest[i : i + self._tuple_size]
                for i in range(0, prefix_len, self._tuple_size)
            ]
            if self._tuple_size > 0
            else []
        )
        encoded = _percent_encode(object_id)
        if len(encoded) > _MAX_ENCODED_LENGTH:
            encoded = f"{encoded[:_MAX_ENCODED_LENGTH]}-{digest}"
        return "/".join([*tuples, encoded])

    def _hash_id(self, object_id: str) -> str:
        h = self._alg.new_hasher()
        h.update(object_id.encode("utf-8"))
        return h.hexdigest()
