"""Batch timestamping state file schema and persistence helpers."""

import atexit
import fcntl
import os
import signal
import socket
import threading
import uuid
from collections.abc import Callable
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from types import FrameType
from typing import TYPE_CHECKING, Any, Literal, cast

import filelock
import msgspec

from ocfl_cli.errors import LockTimeoutError, StagingError
from ocfl_cli.stage import store

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


BATCH_FILE_SCHEMA_VERSION = 1
DRIVER_LEASE_STALE_AFTER_SECONDS = 300
DRIVER_LEASE_HEARTBEAT_SECONDS = 60

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


class DriverLease(
    msgspec.Struct, frozen=True, kw_only=True, forbid_unknown_fields=True
):
    """One command-driver lease holder for a batch."""

    batch_id: str
    pid: int
    host: str
    started_at: str
    heartbeat_at: str
    command: str
    cli_version: str


class DriverLeaseHandle:
    """Owned driver lease with explicit refresh and release operations."""

    def __init__(
        self,
        staging_root: Path,
        lease: DriverLease,
        *,
        heartbeat_interval: float | None,
    ) -> None:
        self._staging_root = staging_root
        self._lease = lease
        self._released = False
        self._stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._signal_registration: _SignalRegistration | None = None
        if heartbeat_interval is not None:
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                args=(heartbeat_interval,),
                daemon=True,
                name=f"ocfl-batch-driver-{lease.batch_id}",
            )
            self._heartbeat_thread.start()

    @property
    def batch_id(self) -> str:
        return self._lease.batch_id

    @property
    def pid(self) -> int:
        return self._lease.pid

    @property
    def host(self) -> str:
        return self._lease.host

    @property
    def started_at(self) -> str:
        return self._lease.started_at

    @property
    def heartbeat_at(self) -> str:
        return self._lease.heartbeat_at

    @property
    def command(self) -> tuple[str, ...]:
        return tuple(self._lease.command.split())

    def refresh(self, *, now: datetime | None = None) -> None:
        """Refresh this holder's heartbeat under the lease refresh lock."""
        if self._released:
            raise StagingError(f"batch {self.batch_id}: driver lease is released")
        now = _coerce_utc(now or datetime.now(UTC))
        lock_path = driver_refresh_lock_path(self._staging_root, self.batch_id)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                current = load_driver_lease(self._staging_root, self.batch_id)
                if not _same_holder(current, self._lease):
                    raise StagingError(
                        f"batch {self.batch_id}: driver lease is no longer held "
                        f"by pid={self.pid} host={self.host}"
                    )
                self._lease = msgspec.structs.replace(
                    self._lease, heartbeat_at=_format_datetime(now)
                )
                _write_driver_lease_exclusive(
                    self._staging_root, self._lease, replace=True
                )
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def release(self) -> None:
        """Release this lease if the current on-disk holder still matches."""
        if self._released:
            return
        self._released = True
        self._stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=5)
        if self._signal_registration is not None:
            self._signal_registration.restore()
            self._signal_registration = None
        path = driver_lease_path(self._staging_root, self.batch_id)
        lock_path = driver_refresh_lock_path(self._staging_root, self.batch_id)
        with open(lock_path, "a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    current = load_driver_lease(self._staging_root, self.batch_id)
                except StagingError:
                    return
                if _same_holder(current, self._lease):
                    path.unlink(missing_ok=True)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _heartbeat_loop(self, heartbeat_interval: float) -> None:
        while not self._stop.wait(heartbeat_interval):
            try:
                self.refresh()
            except StagingError:
                return


SignalHandler = Callable[[int, FrameType | None], None]
SignalAction = int | SignalHandler


@dataclass
class _SignalRegistration:
    handle: DriverLeaseHandle
    previous: dict[signal.Signals, SignalAction]
    installed: dict[signal.Signals, SignalHandler]

    def restore(self) -> None:
        for sig, previous_handler in self.previous.items():
            if signal.getsignal(sig) == self.installed[sig]:
                signal.signal(sig, previous_handler)
        with suppress(ValueError):
            atexit.unregister(self.handle.release)


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


def driver_lease_path(staging_root: Path, batch_id: str) -> Path:
    """Path to `<staging-root>/batches/<batch_id>.driver`."""
    _validate_batch_id(batch_id)
    return batches_dir(staging_root) / f"{batch_id}.driver"


def driver_refresh_lock_path(staging_root: Path, batch_id: str) -> Path:
    """Path to `<batch_id>.driver.refresh-lock`."""
    _validate_batch_id(batch_id)
    return batches_dir(staging_root) / f"{batch_id}.driver.refresh-lock"


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


def load_driver_lease(staging_root: Path, batch_id: str) -> DriverLease:
    """Load a batch command-driver lease."""
    _validate_batch_id(batch_id)
    path = driver_lease_path(staging_root, batch_id)
    try:
        lease = msgspec.json.decode(path.read_bytes(), type=DriverLease)
    except msgspec.ValidationError as e:
        raise StagingError(f"{path}: invalid driver lease: {e}") from None
    except (OSError, msgspec.DecodeError) as e:
        raise StagingError(f"{path}: {e}") from None
    if lease.batch_id != batch_id:
        raise StagingError(
            f"{path}: batch_id {lease.batch_id!r} does not match {batch_id!r}"
        )
    _validate_driver_lease(lease, path=path)
    return lease


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


def save_driver_lease(
    staging_root: Path,
    batch_id: str,
    *,
    pid: int,
    host: str,
    command: tuple[str, ...],
    started_at: datetime,
    heartbeat_at: datetime,
) -> DriverLease:
    """Write a driver lease for tests and recovery tooling."""
    _validate_batch_id(batch_id)
    lease = DriverLease(
        batch_id=batch_id,
        pid=pid,
        host=host,
        started_at=_format_datetime(_coerce_utc(started_at)),
        heartbeat_at=_format_datetime(_coerce_utc(heartbeat_at)),
        command=_format_command(command),
        cli_version=_cli_version(),
    )
    _write_driver_lease_exclusive(staging_root, lease, replace=True)
    return lease


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


@contextmanager
def with_driver_lease(
    staging_root: Path,
    batch_id: str,
    *,
    command: tuple[str, ...],
    heartbeat_interval: float | None = DRIVER_LEASE_HEARTBEAT_SECONDS,
    stale_after: float = DRIVER_LEASE_STALE_AFTER_SECONDS,
) -> Iterator[DriverLeaseHandle]:
    """Acquire a command-driver lease for one batch and release it on exit."""
    handle = acquire_driver_lease(
        staging_root,
        batch_id,
        command=command,
        heartbeat_interval=heartbeat_interval,
        stale_after=stale_after,
    )
    try:
        handle._signal_registration = _register_signal_release(handle)
        yield handle
    finally:
        handle.release()


def acquire_driver_lease(
    staging_root: Path,
    batch_id: str,
    *,
    command: tuple[str, ...],
    heartbeat_interval: float | None = DRIVER_LEASE_HEARTBEAT_SECONDS,
    stale_after: float = DRIVER_LEASE_STALE_AFTER_SECONDS,
) -> DriverLeaseHandle:
    """Acquire a command-driver lease, stealing only if the existing lease is stale."""
    _validate_batch_id(batch_id)
    lease = _new_driver_lease(batch_id, command=command)
    directory = batches_dir(staging_root)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = driver_refresh_lock_path(staging_root, batch_id)
    lock_path.touch(mode=0o600, exist_ok=True)
    with open(lock_path, "a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            try:
                _write_driver_lease_exclusive(staging_root, lease, replace=False)
            except FileExistsError:
                lease = _steal_or_raise_locked(
                    staging_root, lease, stale_after=stale_after
                )
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    return DriverLeaseHandle(staging_root, lease, heartbeat_interval=heartbeat_interval)


def _new_driver_lease(batch_id: str, *, command: tuple[str, ...]) -> DriverLease:
    now = _format_datetime(datetime.now(UTC))
    return DriverLease(
        batch_id=batch_id,
        pid=os.getpid(),
        host=socket.gethostname(),
        started_at=now,
        heartbeat_at=now,
        command=_format_command(command),
        cli_version=_cli_version(),
    )


def _steal_or_raise_locked(
    staging_root: Path,
    replacement: DriverLease,
    *,
    stale_after: float,
    expected: DriverLease | None = None,
) -> DriverLease:
    batch_id = replacement.batch_id
    current = load_driver_lease(staging_root, batch_id)
    if (expected is not None and not _same_holder(current, expected)) or not _is_stale(
        current, stale_after=stale_after
    ):
        raise _lease_held_error(batch_id, current)
    _rename_stolen_lease(staging_root, current)
    _write_driver_lease_exclusive(staging_root, replacement, replace=False)
    return replacement


def _write_driver_lease_exclusive(
    staging_root: Path, lease: DriverLease, *, replace: bool
) -> None:
    path = driver_lease_path(staging_root, lease.batch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    _validate_driver_lease(lease, path=path)
    if replace:
        store._atomic_write_bytes(path, msgspec.json.encode(lease))
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(msgspec.json.encode(lease))
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _rename_stolen_lease(staging_root: Path, lease: DriverLease) -> None:
    path = driver_lease_path(staging_root, lease.batch_id)
    stolen_path = path.with_name(
        f"{path.name}.stolen.{os.getpid()}.{int(datetime.now(UTC).timestamp())}"
    )
    os.replace(path, stolen_path)


def _is_stale(lease: DriverLease, *, stale_after: float) -> bool:
    heartbeat_at = _parse_datetime(lease.heartbeat_at)
    if datetime.now(UTC) - heartbeat_at > timedelta(seconds=stale_after):
        return True
    return lease.host == socket.gethostname() and not _pid_is_alive(lease.pid)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lease_held_error(batch_id: str, lease: DriverLease) -> LockTimeoutError:
    return LockTimeoutError(
        f"batch {batch_id} driver lease is held by pid={lease.pid} "
        f"host={lease.host} started_at={lease.started_at} "
        f"heartbeat_at={lease.heartbeat_at} command={lease.command}"
    )


def _same_holder(left: DriverLease, right: DriverLease) -> bool:
    return (
        left.batch_id == right.batch_id
        and left.pid == right.pid
        and left.host == right.host
        and left.started_at == right.started_at
        and left.command == right.command
        and left.cli_version == right.cli_version
    )


def _validate_driver_lease(lease: DriverLease, *, path: Path) -> None:
    if lease.pid <= 0:
        raise StagingError(f"{path}: driver lease pid must be positive")
    if not lease.host:
        raise StagingError(f"{path}: driver lease host is required")
    if not lease.command:
        raise StagingError(f"{path}: driver lease command is required")
    if not lease.cli_version:
        raise StagingError(f"{path}: driver lease cli_version is required")
    _parse_datetime(lease.started_at)
    _parse_datetime(lease.heartbeat_at)


def _format_command(command: tuple[str, ...]) -> str:
    if not command:
        raise StagingError("driver lease command is required")
    return " ".join(command)


def _cli_version() -> str:
    try:
        return version("ocfl-cli")
    except PackageNotFoundError:
        return "unknown"


def now_utc_iso_z() -> str:
    """UTC timestamp for batch files at seconds precision with a Z suffix."""
    return _format_datetime(datetime.now(UTC))


def _format_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise StagingError(f"invalid driver lease timestamp {value!r}") from None
    if parsed.tzinfo is None:
        raise StagingError(
            f"invalid driver lease timestamp {value!r}: timezone required"
        )
    return parsed.astimezone(UTC)


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise StagingError("driver lease timestamp timezone required")
    return value.astimezone(UTC)


def _register_signal_release(handle: DriverLeaseHandle) -> _SignalRegistration | None:
    if threading.current_thread() is not threading.main_thread():
        return None
    atexit.register(handle.release)
    previous: dict[signal.Signals, SignalAction] = {}
    installed: dict[signal.Signals, SignalHandler] = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous_handler = _signal_action(signal.getsignal(sig))

        def release_and_forward(
            signum: int,
            frame: FrameType | None,
            *,
            previous_handler: SignalAction = previous_handler,
        ) -> None:
            handle.release()
            if previous_handler == signal.SIG_DFL:
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)
            elif previous_handler == signal.SIG_IGN:
                return
            else:
                cast("SignalHandler", previous_handler)(signum, frame)

        previous[sig] = previous_handler
        installed[sig] = release_and_forward
        signal.signal(sig, release_and_forward)
    return _SignalRegistration(handle=handle, previous=previous, installed=installed)


def _signal_action(raw: Any) -> SignalAction:
    if raw is None:
        return signal.SIG_DFL
    if raw in (signal.SIG_DFL, signal.SIG_IGN):
        return raw
    return cast("SignalHandler", raw)


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
