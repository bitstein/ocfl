"""Same-machine locks the CLI takes around commits."""

import hashlib
import tempfile
from contextlib import contextmanager
from pathlib import Path

import filelock

from ocfl_cli.errors import (
    LockTimeoutError,
)

_OBJECT_LOCK_DIR = "ocfl-cli-object-locks"


@contextmanager
def object_commit_lock(
    root_path: str,
    object_id: str,
    *,
    timeout: float,
):
    """Serialize local CLI commits for one storage-root/object pair.

    This is an operator-ergonomics lock: it prevents ordinary same-machine CLI
    commits from racing into retryable stale-head failures. The library still
    enforces optimistic stale-head checks because S3 and non-CLI callers need a
    backend-independent correctness guard.
    """
    key = hashlib.sha256(f"{root_path}\0{object_id}".encode()).hexdigest()
    lock_dir = Path(tempfile.gettempdir()) / _OBJECT_LOCK_DIR
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = filelock.FileLock(str(lock_dir / f"{key}.lock"))
    try:
        lock.acquire(timeout=timeout)
    except filelock.Timeout as e:
        raise LockTimeoutError(
            f"could not acquire commit lock for {object_id!r} within {timeout}s"
        ) from e
    try:
        yield
    finally:
        lock.release()
