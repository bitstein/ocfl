"""Backend-agnostic filesystem abstraction for OCFL."""

from ocfl_lib.fs.protocols import (
    FS,
    CopyFS,
    FileRef,
    FileWalker,
    FsEntry,
    WriteFS,
)

__all__ = [
    "FS",
    "CopyFS",
    "FileRef",
    "FileWalker",
    "FsEntry",
    "WriteFS",
]
