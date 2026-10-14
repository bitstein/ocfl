"""Backend-agnostic filesystem abstraction for OCFL."""

from ocfl_lib.fs._local import LocalFS
from ocfl_lib.fs._open import open_fs
from ocfl_lib.fs._protocols import (
    FS,
    ClaimFS,
    ConditionalFS,
    CopyFS,
    FileRef,
    FileWalker,
    FsEntry,
    Readable,
    WriteFS,
)
from ocfl_lib.fs._utils import copy, read_all, read_text, write_bytes

__all__ = [
    "FS",
    "ClaimFS",
    "ConditionalFS",
    "CopyFS",
    "FileRef",
    "FileWalker",
    "FsEntry",
    "LocalFS",
    "Readable",
    "WriteFS",
    "copy",
    "open_fs",
    "read_all",
    "read_text",
    "write_bytes",
]
