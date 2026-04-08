"""Local filesystem backend for OCFL."""

import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from ocfl_lib.fs.protocols import FileRef, FsEntry, OcflFileTypeError


def _classify(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISBLK(mode):
        return "block_device"
    if stat.S_ISCHR(mode):
        return "char_device"
    return "unknown"


def _check_entry_type(entry: os.DirEntry[str]) -> tuple[bool, bool]:
    """Return (is_dir, is_file) for a scandir entry, refusing symlinks.

    Raises OcflFileTypeError for symlinks, FIFOs, sockets, and devices.
    """
    is_dir = entry.is_dir(follow_symlinks=False)
    is_file = entry.is_file(follow_symlinks=False)
    if not is_dir and not is_file:
        st = entry.stat(follow_symlinks=False)
        kind = _classify(st.st_mode)
        raise OcflFileTypeError(
            f"unsupported file type at {entry.path}: {kind}; "
            "only regular files and directories are allowed"
        )
    return is_dir, is_file


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
        """List directory entries. Raises OcflFileTypeError on symlinks etc."""
        path = self._resolve(name)
        with os.scandir(path) as it:
            for entry in it:
                is_dir, is_file = _check_entry_type(entry)
                yield FsEntry(
                    name=entry.name,
                    is_dir=is_dir,
                    is_file=is_file,
                    size=entry.stat(follow_symlinks=False).st_size if is_file else None,
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

    def copy(self, src: str, dst: str) -> int:
        """Copy a file within this filesystem."""
        src_path = self._resolve(src)
        dst_path = self._resolve(dst)
        shutil.copy2(src_path, dst_path)
        return dst_path.stat().st_size

    # -- FileWalker --

    def __repr__(self) -> str:
        return f"LocalFS({self._root})"

    def walk_files(self, name: str) -> Iterator[FileRef]:
        """Recursively yield FileRef for every file under name.

        Raises OcflFileTypeError if any entry is a symlink, FIFO, socket,
        or device node.
        """
        base = self._resolve(name)
        yield from self._walk(base)

    def _walk(self, path: Path) -> Iterator[FileRef]:
        with os.scandir(path) as it:
            for entry in it:
                is_dir, is_file = _check_entry_type(entry)
                if is_dir:
                    yield from self._walk(Path(entry.path))
                elif is_file:
                    rel = Path(entry.path).relative_to(self._root)
                    yield FileRef(
                        path=rel.as_posix(),
                        size=entry.stat(follow_symlinks=False).st_size,
                    )
