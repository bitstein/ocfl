"""Lock helpers: hold or probe a CLI `flock` the way another process would."""

import fcntl
from contextlib import contextmanager
from pathlib import Path

from ocfl_cli.ports import Runtime
from ocfl_cli.system import SYSTEM_CLOCK, SYSTEM_SLEEPER


@contextmanager
def held_flock(path: Path):
    """Hold an exclusive flock on `path` through a second open file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield


def is_flocked(path: Path) -> bool:
    """Whether someone holds an exclusive flock on `path`."""
    with open(path, "a+b") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return False


def lock_runtime(timeout: float = 1.0, *, sleeper=SYSTEM_SLEEPER) -> Runtime:
    """A `Runtime` for calling the lock helpers directly."""
    return Runtime(
        clock=SYSTEM_CLOCK,
        sleeper=sleeper,
        block_streams=lambda cfg: None,
        lock_timeout=timeout,
    )
