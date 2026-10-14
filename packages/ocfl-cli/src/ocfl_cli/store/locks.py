"""The CLI's locks: one `flock` primitive, waited on for `Runtime.lock_timeout`.

Every lock is an exclusive `flock(2)` on a lock file. The kernel drops it
when its holder exits, however it exits, so no lock can go stale and none
needs a heartbeat (ADR-0005). A taker polls with `LOCK_NB` and waits through
`Runtime.sleeper` (tests wait no real time), then fails with
`LockTimeoutError` (exit 4).
"""

import fcntl
import hashlib
import math
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from ocfl_cli.errors import LockTimeoutError

if TYPE_CHECKING:
    from collections.abc import Generator

    from ocfl_cli.ports import Runtime

POLL_SECONDS = 0.1
_OBJECT_LOCK_DIR = "ocfl-cli-object-locks"


@contextmanager
def flock(path: Path, runtime: Runtime, *, what: str) -> Generator[None]:
    """Hold an exclusive `flock` on `path` (created if missing).

    Waits up to `runtime.lock_timeout` seconds, in `POLL_SECONDS` steps
    through `runtime.sleeper`, then raises `LockTimeoutError` naming `what`.
    Two opens of the same file conflict even within one process.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        polls = math.ceil(runtime.lock_timeout / POLL_SECONDS)
        for poll in range(polls + 1):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if poll == polls:
                    raise LockTimeoutError(
                        f"could not acquire {what} within {runtime.lock_timeout:g}s: "
                        "another process holds it"
                    ) from None
                runtime.sleeper.sleep(runtime.lock_timeout / polls)
        yield
    finally:
        os.close(fd)


def object_lock_path(root_path: str, object_id: str) -> Path:
    key = hashlib.sha256(f"{root_path}\0{object_id}".encode()).hexdigest()
    return Path(tempfile.gettempdir()) / _OBJECT_LOCK_DIR / f"{key}.lock"


@contextmanager
def object_commit_lock(
    root_path: str, object_id: str, runtime: Runtime
) -> Generator[None]:
    """Serialize local CLI commits for one storage-root/object pair.

    This is an operator-ergonomics lock: it prevents ordinary same-machine CLI
    commits from racing into retryable stale-head failures. The library still
    enforces optimistic stale-head checks because S3 and non-CLI callers need a
    backend-independent correctness guard.
    """
    with flock(
        object_lock_path(root_path, object_id),
        runtime,
        what=f"the commit lock for {object_id!r}",
    ):
        yield
