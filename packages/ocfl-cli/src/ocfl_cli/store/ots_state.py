"""`<staging>/ots.json` persistence: load/save, schema migrations, the lock.

The model lives in `ocfl_cli.model.ots_state`.
"""

import logging
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import filelock
import msgspec

from ocfl_cli.errors import LockTimeoutError, StagingError
from ocfl_cli.model.ots_state import (
    OTS_STATE_VERSION,
    OtsState,
)
from ocfl_cli.store import _wire
from ocfl_cli.store import staging as store

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema migrations
# ---------------------------------------------------------------------------

# `_MIGRATIONS[i]` upgrades schema v=i to v=i+1. `load_ots` composes them
# to walk any supported prior version forward to `OTS_STATE_VERSION`.
# Each migration is a single forward step; pure dict-to-dict.
_MIGRATIONS: dict[int, Callable[[dict], dict]] = {}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _ots_path(staging: Path) -> Path:
    return staging / "ots.json"


def has_ots(staging: Path) -> bool:
    """Whether `<staging>/ots.json` exists (the OTS extension is active)."""
    return _ots_path(staging).is_file()


def remove_ots(staging: Path) -> None:
    """Delete `<staging>/ots.json` if present. Idempotent."""
    _ots_path(staging).unlink(missing_ok=True)


def ots_cache_path(staging: Path, digest: str) -> Path:
    """Deterministic on-disk path for cached `.ots` bytes for *digest*."""
    return staging / "ots-cache" / f"{digest}.ots"


def plan_path(staging: Path) -> Path:
    """Path to the serialized `UpdatePlan` written at `timestamp inventory`."""
    return staging / "plan.json"


def now_utc_iso() -> str:
    """RFC-3339 / ISO-8601 UTC timestamp at seconds precision."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def load_ots(staging: Path) -> OtsState:
    """Load `<staging>/ots.json`, running migrations to current schema.

    Raises `StagingError` when the file's `schema_version` is newer than
    `OTS_STATE_VERSION` (newer CLI required) or its shape can't be parsed.
    """
    path = _ots_path(staging)
    try:
        raw = msgspec.json.decode(path.read_bytes())
    except (OSError, msgspec.DecodeError) as e:
        raise StagingError(f"{path}: {e}") from None
    if not isinstance(raw, dict):
        raise StagingError(f"{path}: top-level value must be an object")
    v = raw.get("schema_version", 1)
    if not isinstance(v, int):
        raise StagingError(f"{path}: invalid schema_version {v!r}")
    if v > OTS_STATE_VERSION:
        raise StagingError(
            f"{path}: schema_version={v} is newer than CLI "
            f"(supports up to {OTS_STATE_VERSION}); upgrade the CLI"
        )
    for i in range(v, OTS_STATE_VERSION):
        try:
            migrate = _MIGRATIONS[i]
        except KeyError:
            raise StagingError(
                f"{path}: no migration registered for schema v{i} → v{i + 1}"
            ) from None
        raw = migrate(raw)
        raw["schema_version"] = i + 1
    return _wire.decode_ots_state(raw)


def save_ots(staging: Path, state: OtsState) -> None:
    """Atomically write `state` to `<staging>/ots.json` (temp + fsync + replace)."""
    raw_bytes = msgspec.json.encode(_wire.encode_ots_state(state))
    store.atomic_write_bytes(_ots_path(staging), raw_bytes)


@contextmanager
def with_lock(staging: Path, *, timeout: float = 30) -> Iterator[None]:
    """Acquire `<staging>/.lock` exclusively; raise `LockTimeoutError` on timeout.

    Held by every command that mutates `ots.json`; `stage status` runs
    lock-free against the atomically-rewritten file.
    """
    lock = filelock.FileLock(str(staging / ".lock"))
    try:
        lock.acquire(timeout=timeout)
    except filelock.Timeout as e:
        raise LockTimeoutError(
            f"could not acquire {staging}/.lock within {timeout}s"
        ) from e
    try:
        yield
    finally:
        lock.release()


def apply_category_id(state: OtsState, cfg_category_id: int) -> int:
    """Reconcile config's `proof_category_id` against the persisted one.

    All in-flight jobs in a single `ots.json` must share a category so
    `--retry` / `--upgrade` address the right SP records. On drift, log
    WARNING and keep the persisted value for the rest of this staging
    session; the new config value takes effect on the next fresh staging.
    """
    if state.category_id != cfg_category_id:
        logger.warning(
            "category_id drift: persisted=%d, config=%d; using persisted "
            "value for this staging session (config applies to next fresh "
            "staging after commit/reset)",
            state.category_id,
            cfg_category_id,
        )
    return state.category_id
