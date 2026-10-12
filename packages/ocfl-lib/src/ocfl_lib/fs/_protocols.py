"""FS protocols, FileRef, and FsEntry for backend-agnostic filesystem access."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, BinaryIO, Protocol, runtime_checkable

from ocfl_lib._errors import OcflFileTypeError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import AbstractContextManager


__all__ = [
    "FS",
    "ClaimFS",
    "ConditionalFS",
    "CopyFS",
    "FileRef",
    "FileWalker",
    "FsEntry",
    "OcflFileTypeError",
    "WriteFS",
]


@dataclass(frozen=True, slots=True)
class FileRef:
    """Reference to a file: path relative to FS root, size in bytes."""

    path: str
    size: int


@dataclass(frozen=True, slots=True)
class FsEntry:
    """Single entry from a directory listing."""

    name: str
    is_dir: bool
    is_file: bool
    size: int | None


class FS(Protocol):
    """Read-only filesystem."""

    def open_file(self, name: str) -> BinaryIO: ...
    def read_dir(self, name: str) -> Iterator[FsEntry]: ...
    def exists(self, name: str) -> bool: ...


class WriteFS(FS, Protocol):
    """Filesystem with write operations."""

    def write(self, name: str, data: BinaryIO) -> int: ...
    def remove(self, name: str) -> None: ...
    def remove_all(self, name: str) -> None: ...
    def mkdir(self, name: str) -> None: ...


@runtime_checkable
class CopyFS(WriteFS, Protocol):
    """Filesystem with server-side copy (e.g. same-bucket S3 copy)."""

    def copy(self, src: str, dst: str) -> int: ...


class FileWalker(FS, Protocol):
    """Filesystem that can recursively walk files."""

    def walk_files(self, name: str) -> Iterator[FileRef]: ...


@runtime_checkable
class ClaimFS(WriteFS, Protocol):
    """Local filesystem that claims a version by a create-only rename (ADR-0001)."""

    def rename_noreplace(self, src: str, dst: str) -> None:
        """Rename `src` to `dst` atomically; FileExistsError if `dst` exists.

        Raises OSError (EINVAL, ENOSYS, EXDEV) where the platform or
        filesystem cannot do it.
        """
        ...

    def lock(self, name: str, *, timeout: float) -> AbstractContextManager[None]:
        """Hold an exclusive lock on directory `name`, creating it if missing.

        Raises BlockingIOError if the lock is still held after `timeout` s.
        """
        ...

    def remove_empty_dir(self, name: str) -> bool:
        """Remove directory `name` if it is empty. True if it was removed."""
        ...


@runtime_checkable
class ConditionalFS(WriteFS, Protocol):
    """Object store with create-only and compare-and-swap writes (ADR-0001)."""

    def walk_files(self, name: str) -> Iterator[FileRef]: ...

    def read_tagged(self, name: str) -> tuple[bytes, str]:
        """Read `name` with its entity tag. FileNotFoundError if missing."""
        ...

    def write_new(self, name: str, data: bytes) -> str:
        """Create `name`; FileExistsError if it exists. Returns the new tag."""
        ...

    def write_if_match(self, name: str, data: bytes, tag: str) -> str | None:
        """Replace `name` only if its tag is still `tag`. None if it is not."""
        ...

    def check_conditional_writes(self, probe: str) -> None:
        """Raise UnsupportedBackendError if the store ignores the conditions.

        Writes and then removes the key `probe`.
        """
        ...
