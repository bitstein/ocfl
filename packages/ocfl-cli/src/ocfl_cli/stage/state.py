"""OTS workspace state for staged OCFL versions.

`<staging>/ots.json` schema (v1) plus persistence, locking, and
schema-migration helpers.
"""

import logging
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import filelock
import msgspec

from ocfl_cli.errors import LockTimeoutError, StagingError
from ocfl_cli.stage import _wire, store
from ocfl_cli.stage.fsm import Phase  # noqa: TC001 - msgspec decodes this at runtime.

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


logger = logging.getLogger(__name__)

_OTS_STATE_VERSION = 1


# ---------------------------------------------------------------------------
# OTS state structs
# ---------------------------------------------------------------------------


class VerifiedProof(msgspec.Struct, frozen=True):
    """Shared sub-shape: present in every `content_proofs` entry and in
    `inventory_proof`. Adding a field requires a schema bump on both.
    """

    ots_path: str | None = None
    verified: bool = False
    block_height: int | None = None
    confirmations_at_verify: int | None = None
    verified_at: str | None = None


class Submission(msgspec.Struct, frozen=True):
    """A round of `/api/proof/timestamp/hashes` POSTs."""

    submitted_at: str
    digests: tuple[str, ...]
    status: str  # "submitting" | "submitted" | "failed"
    job_id: str | None = None
    attempts: int = 0


class VerifyJob(msgspec.Struct, frozen=True):
    """An outstanding `verify-batches` job awaiting download/verify."""

    job_id: str
    digests: tuple[str, ...]
    status: str  # "IN_PROGRESS" | "COMPLETED" | "FAILED"


class InventoryProof(msgspec.Struct, frozen=True):
    """`inventory_proof`: inventory digest + version + verify job_id plus a
    nested shared `VerifiedProof`. The JSON wire shape flattens `proof`
    into the top level of `inventory_proof` (see plan §`ots.json` schema)."""

    version: str
    digest: str
    proof: VerifiedProof = msgspec.field(default_factory=VerifiedProof)
    job_id: str | None = None


class OtsState(msgspec.Struct, frozen=True):
    """In-memory representation of `<staging>/ots.json` (schema v1).

    Frozen so callers must rebuild via `msgspec.structs.replace` to advance
    phase or append submissions.

    Persisted via `save_ots` (atomic write-temp + fsync + replace).
    """

    phase: Phase
    category_id: int
    schema_version: int = 1
    pending_digests: tuple[str, ...] = ()
    submissions: tuple[Submission, ...] = ()
    verify_jobs: tuple[VerifyJob, ...] = ()
    content_proofs: dict[str, VerifiedProof] = msgspec.field(default_factory=dict)
    inventory_proof: InventoryProof | None = None


# ---------------------------------------------------------------------------
# Schema migrations
# ---------------------------------------------------------------------------

# `_MIGRATIONS[i]` upgrades schema v=i to v=i+1. `load_ots` composes them
# to walk any supported prior version forward to `_OTS_STATE_VERSION`.
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
    `_OTS_STATE_VERSION` (newer CLI required) or its shape can't be parsed.
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
    if v > _OTS_STATE_VERSION:
        raise StagingError(
            f"{path}: schema_version={v} is newer than CLI "
            f"(supports up to {_OTS_STATE_VERSION}); upgrade the CLI"
        )
    for i in range(v, _OTS_STATE_VERSION):
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
    store._atomic_write_bytes(_ots_path(staging), raw_bytes)


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
