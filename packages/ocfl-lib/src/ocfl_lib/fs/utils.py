"""FS utility functions for common operations."""

from io import BytesIO
from typing import TYPE_CHECKING

from ocfl_lib.fs.protocols import CopyFS, WriteFS

if TYPE_CHECKING:
    from ocfl_lib.fs.protocols import FS


def read_all(fsys: FS, name: str) -> bytes:
    """Read entire file contents as bytes."""
    with fsys.open_file(name) as f:
        return f.read()


def read_text(fsys: FS, name: str, *, encoding: str = "utf-8") -> str:
    """Read entire file contents as text."""
    return read_all(fsys, name).decode(encoding)


def write_bytes(fsys: WriteFS, name: str, data: bytes) -> int:
    """Write bytes to a file."""
    return fsys.write(name, BytesIO(data))


def copy(src_fs: FS, src_name: str, dst_fs: WriteFS, dst_name: str) -> int:
    """Copy a file between filesystems.

    Uses server-side copy if src and dst are the same CopyFS instance.
    """
    if src_fs is dst_fs and isinstance(dst_fs, CopyFS):
        return dst_fs.copy(src_name, dst_name)
    with src_fs.open_file(src_name) as f:
        return dst_fs.write(dst_name, f)
