import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn

from ocfl_cli.config import Config
from ocfl_cli.ports import Runtime
from ocfl_cli.system import SYSTEM_CLOCK, SYSTEM_SLEEPER


@contextmanager
def held_flock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield


def is_flocked(path: Path) -> bool:
    with open(path, "a+b") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return False


def _no_block_streams(cfg: Config) -> NoReturn:
    raise AssertionError("lock helpers open no block stream")


def lock_runtime(timeout: float = 1.0, *, sleeper=SYSTEM_SLEEPER) -> Runtime:
    return Runtime(
        clock=SYSTEM_CLOCK,
        sleeper=sleeper,
        block_streams=_no_block_streams,
        lock_timeout=timeout,
    )
