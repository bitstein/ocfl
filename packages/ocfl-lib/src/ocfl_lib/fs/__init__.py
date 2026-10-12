"""Backend-agnostic filesystem abstraction for OCFL."""

from ocfl_lib.fs.local import LocalFS
from ocfl_lib.fs.protocols import (
    FS,
    CopyFS,
    FileRef,
    FileWalker,
    FsEntry,
    WriteFS,
)
from ocfl_lib.fs.utils import copy, read_all, read_text, write_bytes

__all__ = [
    "FS",
    "CopyFS",
    "FileRef",
    "FileWalker",
    "FsEntry",
    "LocalFS",
    "WriteFS",
    "copy",
    "read_all",
    "read_text",
    "write_bytes",
]
