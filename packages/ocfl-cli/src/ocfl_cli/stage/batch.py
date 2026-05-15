"""Batch timestamping state file schema and persistence helpers."""

import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Literal

import filelock
import msgspec

from ocfl_cli.errors import LockTimeoutError, StagingError
from ocfl_cli.stage import store

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


BATCH_FILE_SCHEMA_VERSION = 1

BatchTarget = Literal["content", "inventory"]
BatchObjectStatus = Literal["prepared", "submitted", "upgraded", "failed", "skipped"]
BatchSubmissionStatus = Literal["pending", "submitted", "failed"]
BatchVerifyJobStatus = Literal["IN_PROGRESS", "COMPLETED", "FAILED", "EXPIRED"]


class BatchObject(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One object row in a batch file."""

    object_id: str
    status: BatchObjectStatus
    digests: tuple[str, ...] = ()
    skipped_reason: str | None = None
    last_error: str | None = None


class BatchSubmission(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One SimpleProof submit chunk tracked by a batch file."""

    chunk_index: int
    digests: tuple[str, ...]
    status: BatchSubmissionStatus
    record_ids: tuple[str, ...] | None = None
    submitted_at: str | None = None
    attempts: int = 0
    last_error: str | None = None


class BatchVerifyJob(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One SimpleProof verify-batches job tracked by a batch file."""

    job_id: str
    digests: tuple[str, ...]
    status: BatchVerifyJobStatus
    submitted_at: str
    results_url: str | None = None
    results_url_expires_at: str | None = None
    attempts: int = 0
    completed_at: str | None = None
    last_error: str | None = None


class BatchFile(msgspec.Struct, frozen=True, kw_only=True, forbid_unknown_fields=True):
    """On-disk batch file schema."""

    schema_version: int = BATCH_FILE_SCHEMA_VERSION
    batch_id: str
    target: BatchTarget
    root_path: str
    category_id: int
    created_at: str
    updated_at: str
    objects: tuple[BatchObject, ...] = ()
    submissions: tuple[BatchSubmission, ...] = ()
    verify_jobs: tuple[BatchVerifyJob, ...] = ()


def new_batch_id() -> str:
    """Return a locally generated UUIDv7 batch id."""
    return str(uuid.uuid7())


def batches_dir(staging_root: Path) -> Path:
    """Directory holding batch JSON and lock files."""
    return staging_root / "batches"


def batch_file_path(staging_root: Path, batch_id: str) -> Path:
    """Path to `<staging-root>/batches/<batch_id>.json`."""
    _validate_batch_id(batch_id)
    return batches_dir(staging_root) / f"{batch_id}.json"


def batch_lock_path(staging_root: Path, batch_id: str) -> Path:
    """Path to `<staging-root>/batches/<batch_id>.lock`."""
    _validate_batch_id(batch_id)
    return batches_dir(staging_root) / f"{batch_id}.lock"


def load_batch(staging_root: Path, batch_id: str) -> BatchFile:
    """Load a batch file, requiring current schema v1."""
    _validate_batch_id(batch_id)
    path = batch_file_path(staging_root, batch_id)
    try:
        raw = msgspec.json.decode(path.read_bytes())
        _validate_required_batch_fields(raw, path=path)
        batch = msgspec.convert(raw, BatchFile)
    except msgspec.ValidationError as e:
        raise StagingError(f"{path}: invalid batch file: {e}") from None
    except (OSError, msgspec.DecodeError) as e:
        raise StagingError(f"{path}: {e}") from None
    if batch.schema_version != BATCH_FILE_SCHEMA_VERSION:
        raise StagingError(
            f"{path}: schema_version={batch.schema_version} is not supported "
            f"(supports {BATCH_FILE_SCHEMA_VERSION})"
        )
    if batch.batch_id != batch_id:
        raise StagingError(
            f"{path}: batch_id {batch.batch_id!r} does not match {batch_id!r}"
        )
    return batch


def save_batch(staging_root: Path, batch: BatchFile) -> None:
    """Atomically write `batch` to its batch file."""
    _validate_batch_id(batch.batch_id)
    if batch.schema_version != BATCH_FILE_SCHEMA_VERSION:
        raise StagingError(
            f"batch {batch.batch_id}: schema_version={batch.schema_version} "
            f"is not supported (supports {BATCH_FILE_SCHEMA_VERSION})"
        )
    directory = batches_dir(staging_root)
    directory.mkdir(parents=True, exist_ok=True)
    store._atomic_write_bytes(
        batch_file_path(staging_root, batch.batch_id), msgspec.json.encode(batch)
    )


@contextmanager
def with_batch_lock(
    staging_root: Path, batch_id: str, *, timeout: float = 30
) -> Iterator[None]:
    """Acquire `<staging-root>/batches/<batch_id>.lock` exclusively."""
    _validate_batch_id(batch_id)
    directory = batches_dir(staging_root)
    directory.mkdir(parents=True, exist_ok=True)
    path = batch_lock_path(staging_root, batch_id)
    lock = filelock.FileLock(str(path))
    try:
        lock.acquire(timeout=timeout)
    except filelock.Timeout as e:
        raise LockTimeoutError(f"could not acquire {path} within {timeout}s") from e
    try:
        yield
    finally:
        lock.release()


def _validate_batch_id(batch_id: str) -> None:
    try:
        parsed = uuid.UUID(batch_id)
    except ValueError:
        raise StagingError(f"invalid batch_id {batch_id!r}") from None
    if parsed.version != 7 or str(parsed) != batch_id:
        raise StagingError(f"invalid batch_id {batch_id!r}: expected UUIDv7")


def _validate_required_batch_fields(raw: Any, *, path: Path) -> None:
    if not isinstance(raw, dict):
        raise StagingError(f"{path}: top-level value must be an object")
    _require_keys(
        raw,
        path=path,
        location="batch file",
        keys=(
            "schema_version",
            "batch_id",
            "target",
            "root_path",
            "category_id",
            "created_at",
            "updated_at",
            "objects",
            "submissions",
            "verify_jobs",
        ),
    )
    _require_sequence(raw, key="objects", path=path, location="batch file")
    _require_sequence(raw, key="submissions", path=path, location="batch file")
    _require_sequence(raw, key="verify_jobs", path=path, location="batch file")
    for index, item in enumerate(raw["objects"]):
        location = f"objects[{index}]"
        _require_object(item, path=path, location=location)
        _require_keys(
            item,
            path=path,
            location=location,
            keys=("object_id", "status", "digests", "skipped_reason", "last_error"),
        )
    for index, item in enumerate(raw["submissions"]):
        location = f"submissions[{index}]"
        _require_object(item, path=path, location=location)
        _require_keys(
            item,
            path=path,
            location=location,
            keys=(
                "chunk_index",
                "digests",
                "status",
                "record_ids",
                "submitted_at",
                "attempts",
                "last_error",
            ),
        )
    for index, item in enumerate(raw["verify_jobs"]):
        location = f"verify_jobs[{index}]"
        _require_object(item, path=path, location=location)
        _require_keys(
            item,
            path=path,
            location=location,
            keys=(
                "job_id",
                "digests",
                "status",
                "results_url",
                "results_url_expires_at",
                "attempts",
                "submitted_at",
                "completed_at",
                "last_error",
            ),
        )


def _require_keys(
    raw: dict[str, Any], *, path: Path, location: str, keys: tuple[str, ...]
) -> None:
    for key in keys:
        if key not in raw:
            raise StagingError(f"{path}: {location}.{key} is required")


def _require_sequence(
    raw: dict[str, Any], *, key: str, path: Path, location: str
) -> None:
    if not isinstance(raw[key], list):
        raise StagingError(f"{path}: {location}.{key} must be an array")


def _require_object(raw: Any, *, path: Path, location: str) -> None:
    if not isinstance(raw, dict):
        raise StagingError(f"{path}: {location} must be an object")
