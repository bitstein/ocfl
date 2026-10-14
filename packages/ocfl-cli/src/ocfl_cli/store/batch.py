"""Batch file persistence and the batch lock.

The model lives in `ocfl_cli.model.batch`. The batch lock (`<id>.lock`, an
exclusive `flock`) is held for a whole batch run: it is the lease that keeps
two drivers off one batch, and the kernel drops it when its holder exits
(ADR-0005).
"""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import msgspec

from ocfl_cli.errors import StagingError
from ocfl_cli.model.batch import (
    BATCH_FILE_SCHEMA_VERSION,
    BatchFile,
)
from ocfl_cli.store import staging as store
from ocfl_cli.store.locks import flock

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from ocfl_cli.ports import Runtime


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


def batch_ids(staging_root: Path) -> list[str]:
    """The ids of every batch file in the staging root, sorted."""
    directory = batches_dir(staging_root)
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.json"))


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
    store.atomic_write_bytes(
        batch_file_path(staging_root, batch.batch_id), msgspec.json.encode(batch)
    )


@contextmanager
def batch_lock(staging_root: Path, batch_id: str, runtime: Runtime) -> Generator[None]:
    """Hold the batch's lock for a whole run (`store.locks.flock`).

    Every command that drives, commits or removes the batch holds it; a
    second one waits `runtime.lock_timeout`, then exits 4. Readers (`status`,
    `ls`) need no lock: the batch file is replaced atomically.
    """
    with flock(
        batch_lock_path(staging_root, batch_id), runtime, what=f"batch {batch_id}"
    ):
        yield


def utc_iso_z(now: datetime) -> str:
    """`now` for batch files: UTC at seconds precision with a Z suffix."""
    return now.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


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
