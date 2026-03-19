"""OCFL digest algorithms and computation."""

import enum
import hashlib
import re
from functools import cached_property
from typing import TYPE_CHECKING, BinaryIO, Protocol

if TYPE_CHECKING:
    from pathlib import Path

_CHUNK_SIZE = 32 * 1024


class Hasher(Protocol):
    """Protocol for hashlib-compatible hash objects."""

    def update(self, data: bytes, /) -> None: ...
    def hexdigest(self) -> str: ...
    def digest(self) -> bytes: ...
    def copy(self) -> Hasher: ...


class DigestAlgorithm(enum.Enum):
    """OCFL-supported digest algorithms."""

    SHA512 = ("sha512", "sha512", 128)
    SHA256 = ("sha256", "sha256", 64)
    SHA1 = ("sha1", "sha1", 40)
    MD5 = ("md5", "md5", 32)
    BLAKE2B_512 = ("blake2b-512", "blake2b", 128, 64)
    BLAKE2B_256 = ("blake2b-256", "blake2b", 64, 32)
    BLAKE2B_160 = ("blake2b-160", "blake2b", 40, 20)
    SHA512_256 = ("sha512/256", "sha512_256", 64)

    def __init__(
        self,
        ocfl_name: str,
        hashlib_name: str,
        hex_length: int,
        digest_size: int | None = None,
    ):
        self.ocfl_name = ocfl_name
        self.hashlib_name = hashlib_name
        self.hex_length = hex_length
        self._digest_size = digest_size

    @classmethod
    def from_ocfl_name(cls, name: str) -> DigestAlgorithm:
        for member in cls:
            if member.ocfl_name == name:
                return member
        raise ValueError(f"unknown digest algorithm: {name!r}")

    @property
    def is_spec_algorithm(self) -> bool:
        """True for sha512 and sha256 only."""
        return self in (DigestAlgorithm.SHA512, DigestAlgorithm.SHA256)

    @cached_property
    def regex(self) -> re.Pattern[str]:
        """Compiled pattern matching valid hex digest."""
        return re.compile(f"^[a-fA-F0-9]{{{self.hex_length}}}$")

    def new_hasher(self) -> Hasher:
        """Return correctly configured hashlib hash object."""
        if self._digest_size is not None:
            return hashlib.blake2b(digest_size=self._digest_size)
        return hashlib.new(self.hashlib_name)


def file_digest(path: Path, alg: DigestAlgorithm) -> str:
    """Compute hex digest of a file. Stdlib handles chunking."""
    with open(path, "rb") as f:
        return hashlib.file_digest(f, alg.hashlib_name).hexdigest()


def stream_digest(stream: BinaryIO, alg: DigestAlgorithm) -> str:
    """Compute hex digest of a binary stream using 32KB chunks."""
    h = alg.new_hasher()
    while chunk := stream.read(_CHUNK_SIZE):
        h.update(chunk)
    return h.hexdigest()


def bytes_digest(data: bytes, alg: DigestAlgorithm) -> str:
    """Compute hex digest of bytes."""
    h = alg.new_hasher()
    h.update(data)
    return h.hexdigest()
