"""Local filesystem backend for OCFL."""

import ctypes
import ctypes.util
import errno
import fcntl
import functools
import os
import shutil
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from ocfl_lib.fs._protocols import (
    ClaimFS,
    CopyFS,
    FileRef,
    FileWalker,
    FsEntry,
    OcflFileTypeError,
)


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
    from collections.abc import Callable, Iterator

_CHUNK_SIZE = 32 * 1024

_AT_FDCWD = -100
_RENAME_NOREPLACE = 1


@functools.cache
def _renameat2() -> Callable[..., int] | None:
    if sys.platform != "linux":
        return None
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        fn = libc.renameat2
    except OSError, AttributeError:
        return None
    fn.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    fn.restype = ctypes.c_int
    return fn


def _renameat2_noreplace(src: Path, dst: Path) -> None:
    fn = _renameat2()
    if fn is None:
        raise OSError(errno.ENOSYS, "renameat2 is not available on this platform")
    rc = fn(
        _AT_FDCWD,
        os.fsencode(src),
        _AT_FDCWD,
        os.fsencode(dst),
        _RENAME_NOREPLACE,
    )
    if rc != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(src), None, str(dst))


def _same_inode(path: Path, fd: int) -> bool:
    try:
        st = path.stat()
    except FileNotFoundError:
        return False
    fst = os.fstat(fd)
    return (st.st_dev, st.st_ino) == (fst.st_dev, fst.st_ino)


class LocalFS(ClaimFS, CopyFS, FileWalker):
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

    def exists(self, name: str) -> bool:
        """Return True if a file or directory exists at name."""
        return self._resolve(name).exists()

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
        target.parent.mkdir(parents=True, exist_ok=True)
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
            os.chmod(tmp_path, 0o644)
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
        """Create directory, including parents.

        Safe under concurrent invocation: ``os.makedirs(exist_ok=True)``
        is idempotent on POSIX.
        """
        os.makedirs(self._resolve(name), exist_ok=True)

    # -- CopyFS --

    def copy(self, src: str, dst: str) -> int:
        """Copy a file within this filesystem."""
        src_path = self._resolve(src)
        dst_path = self._resolve(dst)
        shutil.copy2(src_path, dst_path)
        return dst_path.stat().st_size

    # -- ClaimFS --

    def rename_noreplace(self, src: str, dst: str) -> None:
        """Rename atomically, failing with FileExistsError if `dst` exists.

        Uses Linux renameat2(RENAME_NOREPLACE). Raises OSError with ENOSYS,
        EINVAL or EXDEV where the platform or filesystem cannot do it.
        """
        _renameat2_noreplace(self._resolve(src), self._resolve(dst))

    @contextmanager
    def lock(self, name: str, *, timeout: float = 0.0) -> Iterator[None]:
        """Hold an exclusive flock on directory `name`, creating it if missing.

        The directory may be removed while locked; a waiter that then gets the
        lock on the removed directory retries on the new one. Raises
        BlockingIOError if the lock is still held after `timeout` seconds.
        """
        path = self._resolve(name)
        deadline = time.monotonic() + timeout
        while True:
            try:
                path.mkdir(parents=True, exist_ok=True)
                fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            except FileNotFoundError:
                continue  # a parent was removed under us
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
                continue
            if _same_inode(path, fd):
                break
            os.close(fd)
        try:
            yield
        finally:
            os.close(fd)

    def remove_empty_dir(self, name: str) -> bool:
        """Remove directory `name` if it is empty. True if it was removed."""
        try:
            os.rmdir(self._resolve(name))
        except OSError:
            return False
        return True

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
