"""OTS workspace state for staged OCFL versions.

`<staging>/ots.json` schema (v1) plus persistence, locking, and
schema-migration helpers.
"""

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import filelock

from ocfl_cli.errors import LockTimeoutError, StagingError
from ocfl_cli.stage import store
from ocfl_cli.stage.fsm import Phase

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OTS state dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerifiedProof:
    """Shared sub-shape: present in every `content_proofs` entry and in
    `inventory_proof`. Adding a field requires a schema bump on both.
    """

    ots_path: str | None = None
    verified: bool = False
    block_height: int | None = None
    confirmations_at_verify: int | None = None
    verified_at: str | None = None


@dataclass(frozen=True, slots=True)
class Submission:
    """A round of `/api/proof/timestamp/hashes` POSTs."""

    submitted_at: str
    digests: tuple[str, ...]
    status: str  # "submitting" | "submitted" | "failed"
    job_id: str | None = None
    attempts: int = 0


@dataclass(frozen=True, slots=True)
class VerifyJob:
    """An outstanding `verify-batches` job awaiting download/verify."""

    job_id: str
    digests: tuple[str, ...]
    status: str  # "IN_PROGRESS" | "COMPLETED" | "FAILED"


@dataclass(frozen=True, slots=True)
class InventoryProof:
    """`inventory_proof`: inventory digest + version + verify job_id plus a
    nested shared `VerifiedProof`. The JSON wire shape flattens `proof`
    into the top level of `inventory_proof` (see plan §`ots.json` schema)."""

    version: str
    digest: str
    proof: VerifiedProof = field(default_factory=VerifiedProof)
    job_id: str | None = None


@dataclass(frozen=True)
class OtsState:
    """In-memory representation of `<staging>/ots.json` (schema v1).

    Frozen so callers must rebuild via `dataclasses.replace` to advance phase
    or append submissions. Iterable fields are normalized to tuples in
    `__post_init__` so callers may pass either a list or a tuple.
    `content_proofs` is a dict (accepted slotless).

    Persisted via `save_ots` (atomic write-temp + fsync + replace).
    """

    phase: Phase
    category_id: int
    schema_version: int = 1
    pending_digests: tuple[str, ...] = ()
    submissions: tuple[Submission, ...] = ()
    verify_jobs: tuple[VerifyJob, ...] = ()
    content_proofs: dict[str, VerifiedProof] = field(default_factory=dict)
    inventory_proof: InventoryProof | None = None

    def __post_init__(self) -> None:
        for name in ("pending_digests", "submissions", "verify_jobs"):
            v = getattr(self, name)
            if not isinstance(v, tuple):
                object.__setattr__(self, name, tuple(v))


# ---------------------------------------------------------------------------
# Schema migrations
# ---------------------------------------------------------------------------

_OTS_STATE_VERSION = 1

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
        raw = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as e:
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
    return _parse(raw)


def save_ots(staging: Path, state: OtsState) -> None:
    """Atomically write `state` to `<staging>/ots.json` (temp + fsync + replace)."""
    raw_bytes = json.dumps(_serialize(state), sort_keys=True).encode()
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


# ---------------------------------------------------------------------------
# Internal serialize / parse
# ---------------------------------------------------------------------------


def _verified_proof_to_dict(p: VerifiedProof) -> dict[str, Any]:
    return {
        "ots_path": p.ots_path,
        "verified": p.verified,
        "block_height": p.block_height,
        "confirmations_at_verify": p.confirmations_at_verify,
        "verified_at": p.verified_at,
    }


def _verified_proof_from_dict(d: dict[str, Any]) -> VerifiedProof:
    return VerifiedProof(
        ots_path=d.get("ots_path"),
        verified=bool(d.get("verified", False)),
        block_height=d.get("block_height"),
        confirmations_at_verify=d.get("confirmations_at_verify"),
        verified_at=d.get("verified_at"),
    )


def _serialize(state: OtsState) -> dict[str, Any]:
    out: dict[str, Any] = {
        "schema_version": state.schema_version,
        "phase": state.phase.value,
        "category_id": state.category_id,
        "pending_digests": list(state.pending_digests),
        "submissions": [
            {
                "submitted_at": s.submitted_at,
                "digests": list(s.digests),
                "status": s.status,
                "job_id": s.job_id,
                "attempts": s.attempts,
            }
            for s in state.submissions
        ],
        "verify_jobs": [
            {
                "job_id": j.job_id,
                "digests": list(j.digests),
                "status": j.status,
            }
            for j in state.verify_jobs
        ],
        "content_proofs": {
            d: _verified_proof_to_dict(p) for d, p in state.content_proofs.items()
        },
        "inventory_proof": None,
    }
    if state.inventory_proof is not None:
        ip = state.inventory_proof
        out["inventory_proof"] = {
            "version": ip.version,
            "digest": ip.digest,
            "job_id": ip.job_id,
            **_verified_proof_to_dict(ip.proof),
        }
    return out


def _parse(raw: dict[str, Any]) -> OtsState:
    """Decode a dict already at `_OTS_STATE_VERSION`. No I/O, no migrations."""
    try:
        phase = Phase(raw["phase"])
    except (KeyError, ValueError) as e:
        raise StagingError(f"ots.json: invalid 'phase': {e}") from None
    try:
        category_id = int(raw["category_id"])
    except (KeyError, TypeError, ValueError) as e:
        raise StagingError(f"ots.json: invalid 'category_id': {e}") from None
    pending = list(raw.get("pending_digests") or [])

    submissions: list[Submission] = []
    for s in raw.get("submissions") or []:
        try:
            submissions.append(
                Submission(
                    submitted_at=s["submitted_at"],
                    digests=tuple(s["digests"]),
                    status=s["status"],
                    job_id=s.get("job_id"),
                    attempts=int(s.get("attempts", 0)),
                )
            )
        except (KeyError, TypeError, ValueError) as e:
            raise StagingError(f"ots.json: invalid submission entry: {e}") from None

    verify_jobs: list[VerifyJob] = []
    for j in raw.get("verify_jobs") or []:
        try:
            verify_jobs.append(
                VerifyJob(
                    job_id=j["job_id"],
                    digests=tuple(j["digests"]),
                    status=j["status"],
                )
            )
        except (KeyError, TypeError, ValueError) as e:
            raise StagingError(f"ots.json: invalid verify_job entry: {e}") from None

    content_proofs: dict[str, VerifiedProof] = {}
    for d, p in (raw.get("content_proofs") or {}).items():
        if not isinstance(p, dict):
            raise StagingError(f"ots.json: content_proofs[{d}] must be an object")
        content_proofs[d] = _verified_proof_from_dict(p)

    inv_proof: InventoryProof | None = None
    ip_raw = raw.get("inventory_proof")
    if ip_raw is not None:
        if not isinstance(ip_raw, dict):
            raise StagingError("ots.json: inventory_proof must be an object")
        try:
            inv_proof = InventoryProof(
                version=ip_raw["version"],
                digest=ip_raw["digest"],
                job_id=ip_raw.get("job_id"),
                proof=_verified_proof_from_dict(ip_raw),
            )
        except (KeyError, TypeError) as e:
            raise StagingError(f"ots.json: invalid inventory_proof: {e}") from None

    return OtsState(
        schema_version=int(raw.get("schema_version", _OTS_STATE_VERSION)),
        phase=phase,
        category_id=category_id,
        pending_digests=tuple(pending),
        submissions=tuple(submissions),
        verify_jobs=tuple(verify_jobs),
        content_proofs=content_proofs,
        inventory_proof=inv_proof,
    )
