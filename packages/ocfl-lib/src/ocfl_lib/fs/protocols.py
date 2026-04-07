"""FS protocols, FileRef, and FsEntry for backend-agnostic filesystem access."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, BinaryIO, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Iterator


class OcflFileTypeError(ValueError):
    """Raised when a non-regular, non-directory entry is encountered.

    Archives must contain only regular files and directories. Symlinks,
    FIFOs, sockets, and device nodes are rejected.
    """


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


@runtime_checkable
class FS(Protocol):
    """Read-only filesystem."""

    def open_file(self, name: str) -> BinaryIO: ...
    def read_dir(self, name: str) -> Iterator[FsEntry]: ...


@runtime_checkable
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


@runtime_checkable
class FileWalker(FS, Protocol):
    """Filesystem that can recursively walk files."""

    def walk_files(self, name: str) -> Iterator[FileRef]: ...
