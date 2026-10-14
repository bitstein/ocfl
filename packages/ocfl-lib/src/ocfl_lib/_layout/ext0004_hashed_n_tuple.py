import msgspec

from ocfl_lib._digest import DigestAlgorithm
from ocfl_lib._errors import InvalidLayoutConfig
from ocfl_lib._layout.types import Layout

EXTENSION_NAME = "0004-hashed-n-tuple-storage-layout"


class HashedNTupleConfig(msgspec.Struct, rename="camel"):
    extension_name: str = EXTENSION_NAME
    digest_algorithm: str = "sha256"
    tuple_size: int = 3
    number_of_tuples: int = 3
    short_object_root: bool = False


class HashedNTupleLayout(Layout):
    """OCFL 0004: hashed n-tuple storage layout."""

    def __init__(
        self,
        *,
        digest_algorithm: str = "sha256",
        tuple_size: int = 3,
        number_of_tuples: int = 3,
        short_object_root: bool = False,
    ) -> None:
        self._alg = DigestAlgorithm.from_ocfl_name(digest_algorithm)
        self._tuple_size = tuple_size
        self._n_tuples = number_of_tuples
        self._short_root = short_object_root
        self._validate()

    def _validate(self) -> None:
        ts, nt = self._tuple_size, self._n_tuples
        if (ts == 0) != (nt == 0):
            raise InvalidLayoutConfig(
                "tupleSize and numberOfTuples must both be 0 or both non-zero"
            )
        prefix_len = ts * nt
        if prefix_len > self._alg.hex_length:
            raise InvalidLayoutConfig(
                f"tupleSize*numberOfTuples ({prefix_len}) exceeds "
                f"digest hex length ({self._alg.hex_length})"
            )
        if self._short_root and prefix_len == self._alg.hex_length:
            raise InvalidLayoutConfig(
                "shortObjectRoot invalid when tuples cover full digest"
            )

    @property
    def name(self) -> str:
        return EXTENSION_NAME

    @property
    def config(self) -> dict:
        cfg = HashedNTupleConfig(
            digest_algorithm=self._alg.ocfl_name,
            tuple_size=self._tuple_size,
            number_of_tuples=self._n_tuples,
            short_object_root=self._short_root,
        )
        return msgspec.to_builtins(cfg)

    @classmethod
    def from_config(cls, config: dict | None = None) -> HashedNTupleLayout:
        if config is None:
            return cls()
        cfg = msgspec.convert(config, HashedNTupleConfig)
        return cls(
            digest_algorithm=cfg.digest_algorithm,
            tuple_size=cfg.tuple_size,
            number_of_tuples=cfg.number_of_tuples,
            short_object_root=cfg.short_object_root,
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

        root = digest[prefix_len:] if self._short_root else digest
        parts = [*tuples, root]
        return "/".join(parts)

    def _hash_id(self, object_id: str) -> str:
        h = self._alg.new_hasher()
        h.update(object_id.encode("utf-8"))
        return h.hexdigest()
