"""`<staging>/ots.json` persistence: load/save and the lock.

The model lives in `ocfl_cli.model.ots_state`.
"""

import logging
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import msgspec

from ocfl_cli.errors import StagingError
from ocfl_cli.model.ots_state import (
    OTS_STATE_VERSION,
    OtsState,
)
from ocfl_cli.store import _wire
from ocfl_cli.store import staging as store
from ocfl_cli.store.locks import flock

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from ocfl_cli.ports import Runtime


logger = logging.getLogger(__name__)


def _ots_path(staging: Path) -> Path:
    return staging / "ots.json"


def has_ots(staging: Path) -> bool:
    return _ots_path(staging).is_file()


def remove_ots(staging: Path) -> None:
    _ots_path(staging).unlink(missing_ok=True)


def ots_cache_path(staging: Path, digest: str) -> Path:
    return staging / "ots-cache" / f"{digest}.ots"


def plan_path(staging: Path) -> Path:
    return staging / "plan.json"


def utc_iso(now: datetime) -> str:
    return now.astimezone(UTC).isoformat(timespec="seconds")


def load_ots(staging: Path) -> OtsState:
    """Load `<staging>/ots.json`.

    Raises `StagingError` when the file's `schema_version` is not
    `OTS_STATE_VERSION` or its shape can't be parsed.
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
    if v < OTS_STATE_VERSION:
        raise StagingError(
            f"{path}: schema_version={v} predates CLI schema {OTS_STATE_VERSION}; "
            "discard the staging (`stage discard --force`) and stage again"
        )
    return _wire.decode_ots_state(raw)


def save_ots(staging: Path, state: OtsState) -> None:
    raw_bytes = msgspec.json.encode(_wire.encode_ots_state(state))
    store.atomic_write_bytes(_ots_path(staging), raw_bytes)


@contextmanager
def with_lock(staging: Path, runtime: Runtime) -> Generator[None]:
    with flock(staging / ".lock", runtime, what=f"{staging}/.lock"):
        yield


def apply_category_id(state: OtsState, cfg_category_id: int) -> int:
    """Reconcile config's `proof_category_id` against the persisted one.

    All in-flight jobs in a single `ots.json` must share a category so
    `stage retry` / `stage upgrade` address the right SP records. On drift, log
    WARNING and keep the persisted value for the rest of this staging
    session; the new config value takes effect on the next fresh staging.
    """
    if state.category_id != cfg_category_id:
        logger.warning(
            "category_id drift: persisted=%d, config=%d; using persisted "
            "value for this staging session (config applies to next fresh "
            "staging after commit/discard)",
            state.category_id,
            cfg_category_id,
        )
    return state.category_id
