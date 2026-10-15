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


def _require_dir_or_file(entry: os.DirEntry[str]) -> bool:
    is_dir = entry.is_dir(follow_symlinks=False)
    if not is_dir and not entry.is_file(follow_symlinks=False):
        st = entry.stat(follow_symlinks=False)
        kind = _classify(st.st_mode)
        raise OcflFileTypeError(
            f"unsupported file type at {entry.path}: {kind}; "
            "only regular files and directories are allowed"
        )
    return is_dir


if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator

    from ocfl_lib.fs._protocols import Readable

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


def _fsync_dir(path: Path) -> None:
    """Make the entries of directory `path` (creates, renames, removes) durable."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _makedirs_durable(path: Path) -> None:
    missing = []
    p = path
    while not p.is_dir():
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        try:
            d.mkdir()
        except FileExistsError:
            if not d.is_dir():
                raise
            continue
        _fsync_dir(d.parent)


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
        if os.path.isabs(name):
            raise ValueError(f"absolute path not allowed: {name!r}")
        resolved = (self._root / name).resolve()
        if not resolved.is_relative_to(self._root):
            raise ValueError(f"path escapes outside root: {name!r}")
        return resolved

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
                is_dir = _require_dir_or_file(entry)
                is_file = not is_dir
                yield FsEntry(
                    name=entry.name,
                    is_dir=is_dir,
                    is_file=is_file,
                    size=entry.stat(follow_symlinks=False).st_size if is_file else None,
                )

    def write(self, name: str, data: Readable) -> int:
        """Write data to a file atomically and durably.

        The bytes are fsynced before the rename, and the directory after it,
        so a power loss leaves the old file or the new one.
        """
        target = self._resolve(name)
        _makedirs_durable(target.parent)
        fd = None
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(dir=target.parent)
            total = 0
            while chunk := data.read(_CHUNK_SIZE):
                os.write(fd, chunk)
                total += len(chunk)
            os.fchmod(fd, 0o644)
            os.fsync(fd)
            os.close(fd)
            fd = None
            os.replace(tmp_path, target)
            _fsync_dir(target.parent)
            return total
        except BaseException:
            if fd is not None:
                os.close(fd)
            if tmp_path is not None and os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def remove(self, name: str) -> None:
        """Remove a file."""
        path = self._resolve(name)
        os.remove(path)
        _fsync_dir(path.parent)

    def remove_all(self, name: str) -> None:
        """Remove a file or directory tree."""
        path = self._resolve(name)
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            os.remove(path)
        else:
            raise FileNotFoundError(f"path not found: {name!r}")
        _fsync_dir(path.parent)

    def mkdir(self, name: str) -> None:
        """Create directory, including parents.

        Safe under concurrent invocation. Each new directory is made durable
        by an fsync of its parent.
        """
        _makedirs_durable(self._resolve(name))

    def copy(self, src: str, dst: str) -> int:
        """Copy a file within this filesystem."""
        src_path = self._resolve(src)
        dst_path = self._resolve(dst)
        shutil.copy2(src_path, dst_path)
        _fsync_file(dst_path)
        _fsync_dir(dst_path.parent)
        return dst_path.stat().st_size

    def rename_noreplace(self, src: str, dst: str) -> None:
        """Rename atomically, failing with FileExistsError if `dst` exists.

        Uses Linux renameat2(RENAME_NOREPLACE). Raises OSError with ENOSYS,
        EINVAL or EXDEV where the platform or filesystem cannot do it.
        """
        src_path, dst_path = self._resolve(src), self._resolve(dst)
        _renameat2_noreplace(src_path, dst_path)
        _fsync_dir(dst_path.parent)
        if src_path.parent != dst_path.parent:
            _fsync_dir(src_path.parent)

    @contextmanager
    def lock(self, name: str, *, timeout: float = 0.0) -> Generator[None]:
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
                continue
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
        path = self._resolve(name)
        try:
            os.rmdir(path)
        except OSError:
            return False
        _fsync_dir(path.parent)
        return True

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
                if _require_dir_or_file(entry):
                    yield from self._walk(Path(entry.path))
                else:
                    rel = Path(entry.path).relative_to(self._root)
                    yield FileRef(
                        path=rel.as_posix(),
                        size=entry.stat(follow_symlinks=False).st_size,
                    )
