"""Local filesystem backend for OCFL."""

import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from ocfl_lib.fs.protocols import FileRef, FsEntry

if TYPE_CHECKING:
    from collections.abc import Iterator

_CHUNK_SIZE = 32 * 1024


class LocalFS:
    """Local filesystem implementing WriteFS, CopyFS, and FileWalker."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def _resolve(self, name: str) -> Path:
        """Resolve a relative path to an absolute path, validating safety."""
        if os.path.isabs(name):
            raise ValueError(f"absolute path not allowed: {name!r}")
        resolved = (self._root / name).resolve()
        if not resolved.is_relative_to(self._root):
            raise ValueError(f"path escapes outside root: {name!r}")
        return resolved

    # -- FS --

    def open_file(self, name: str) -> BinaryIO:
        """Open a file for reading."""
        return open(self._resolve(name), "rb")

    def read_dir(self, name: str) -> Iterator[FsEntry]:
        """List directory entries."""
        path = self._resolve(name)
        with os.scandir(path) as it:
            for entry in it:
                yield FsEntry(
                    name=entry.name,
                    is_dir=entry.is_dir(),
                    is_file=entry.is_file(),
                    size=entry.stat().st_size if entry.is_file() else 0,
                )

    # -- WriteFS --

    def write(self, name: str, data: BinaryIO) -> int:
        """Write data to a file atomically."""
        target = self._resolve(name)
        fd = None
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(dir=target.parent)
            total = 0
            while chunk := data.read(_CHUNK_SIZE):
                os.write(fd, chunk)
                total += len(chunk)
            os.close(fd)
            fd = None
            os.replace(tmp_path, target)
            return total
        except BaseException:
            if fd is not None:
                os.close(fd)
            if tmp_path is not None and os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def remove(self, name: str) -> None:
        """Remove a file."""
        os.remove(self._resolve(name))

    def remove_all(self, name: str) -> None:
        """Remove a file or directory tree."""
        path = self._resolve(name)
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            os.remove(path)
        else:
            raise FileNotFoundError(f"path not found: {name!r}")

    def mkdir(self, name: str) -> None:
        """Create directory, including parents."""
        os.makedirs(self._resolve(name), exist_ok=True)

    # -- CopyFS --

    def copy(self, dst: str, src: str) -> int:
        """Copy a file within this filesystem."""
        src_path = self._resolve(src)
        dst_path = self._resolve(dst)
        shutil.copy2(src_path, dst_path)
        return dst_path.stat().st_size

    # -- FileWalker --

    def walk_files(self, dir: str) -> Iterator[FileRef]:
        """Recursively yield FileRef for every file under dir."""
        base = self._resolve(dir)
        for dirpath, _dirnames, filenames in os.walk(base):
            for fname in filenames:
                full = Path(dirpath) / fname
                rel = full.relative_to(self._root)
                yield FileRef(
                    path=rel.as_posix(),
                    size=full.stat().st_size,
                )
