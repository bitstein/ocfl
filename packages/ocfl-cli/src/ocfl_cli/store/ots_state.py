import logging
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NewType

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

    from ocfl_cli.ports import Runtime


logger = logging.getLogger(__name__)

LockedStaging = NewType("LockedStaging", Path)


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
def with_lock(staging: Path, runtime: Runtime) -> Generator[LockedStaging]:
    with flock(staging / ".lock", runtime, what=f"{staging}/.lock"):
        yield LockedStaging(staging)


def persisted_category_id(state: OtsState, cfg_category_id: int) -> int:
    if state.category_id != cfg_category_id:
        logger.warning(
            "category_id drift: persisted=%d, config=%d; using persisted "
            "value for this staging session (config applies to next fresh "
            "staging after commit/discard)",
            state.category_id,
            cfg_category_id,
        )
    return state.category_id
