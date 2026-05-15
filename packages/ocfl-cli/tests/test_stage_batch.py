"""Batch timestamping state file helpers."""

import json
import os
import signal
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from ocfl_cli.errors import LockTimeoutError, StagingError
from ocfl_cli.stage import store as store_mod
from ocfl_cli.stage.batch import (
    BatchFile,
    BatchObject,
    BatchSubmission,
    BatchVerifyJob,
    batch_file_path,
    batch_lock_path,
    driver_lease_path,
    driver_refresh_lock_path,
    load_batch,
    load_driver_lease,
    new_batch_id,
    save_batch,
    save_driver_lease,
    with_batch_lock,
    with_driver_lease,
)


def test_batch_file_round_trips_required_schema(tmp_path: Path):
    batch_id = new_batch_id()
    batch = BatchFile(
        batch_id=batch_id,
        target="content",
        root_path="/archive/root",
        category_id=641,
        created_at="2026-05-12T10:00:00Z",
        updated_at="2026-05-12T10:05:00Z",
        objects=(
            BatchObject(
                object_id="obj:1",
                status="submitted",
                digests=("sha256:a", "sha256:b"),
            ),
        ),
        submissions=(
            BatchSubmission(
                chunk_index=0,
                digests=("sha256:a", "sha256:b"),
                status="submitted",
                record_ids=("rec-a", "rec-b"),
                submitted_at="2026-05-12T10:01:00Z",
                attempts=1,
            ),
        ),
        verify_jobs=(
            BatchVerifyJob(
                job_id="verify-1",
                digests=("sha256:a", "sha256:b"),
                status="IN_PROGRESS",
                attempts=1,
                submitted_at="2026-05-12T10:03:00Z",
            ),
        ),
    )

    save_batch(tmp_path, batch)

    assert load_batch(tmp_path, batch_id) == batch
    assert uuid.UUID(batch_id).version == 7
    assert batch_file_path(tmp_path, batch_id) == tmp_path / "batches" / (
        f"{batch_id}.json"
    )
    assert batch_lock_path(tmp_path, batch_id) == tmp_path / "batches" / (
        f"{batch_id}.lock"
    )
    raw = json.loads(batch_file_path(tmp_path, batch_id).read_bytes())
    assert raw == {
        "schema_version": 1,
        "batch_id": batch_id,
        "target": "content",
        "root_path": "/archive/root",
        "category_id": 641,
        "created_at": "2026-05-12T10:00:00Z",
        "updated_at": "2026-05-12T10:05:00Z",
        "objects": [
            {
                "object_id": "obj:1",
                "status": "submitted",
                "digests": ["sha256:a", "sha256:b"],
                "skipped_reason": None,
                "last_error": None,
            }
        ],
        "submissions": [
            {
                "chunk_index": 0,
                "digests": ["sha256:a", "sha256:b"],
                "status": "submitted",
                "record_ids": ["rec-a", "rec-b"],
                "submitted_at": "2026-05-12T10:01:00Z",
                "attempts": 1,
                "last_error": None,
            }
        ],
        "verify_jobs": [
            {
                "job_id": "verify-1",
                "digests": ["sha256:a", "sha256:b"],
                "status": "IN_PROGRESS",
                "results_url": None,
                "results_url_expires_at": None,
                "attempts": 1,
                "submitted_at": "2026-05-12T10:03:00Z",
                "completed_at": None,
                "last_error": None,
            }
        ],
    }


def test_save_batch_keeps_previous_file_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    batch_id = new_batch_id()
    original = BatchFile(
        batch_id=batch_id,
        target="content",
        root_path="/archive/root",
        category_id=641,
        created_at="2026-05-12T10:00:00Z",
        updated_at="2026-05-12T10:00:00Z",
    )
    replacement = BatchFile(
        batch_id=batch_id,
        target="content",
        root_path="/archive/root",
        category_id=641,
        created_at="2026-05-12T10:00:00Z",
        updated_at="2026-05-12T10:05:00Z",
        objects=(BatchObject(object_id="obj:1", status="prepared"),),
    )
    save_batch(tmp_path, original)
    before = batch_file_path(tmp_path, batch_id).read_bytes()

    def fail_replace(src: Path | str, dst: Path | str) -> None:
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(store_mod.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated crash"):
        save_batch(tmp_path, replacement)

    assert batch_file_path(tmp_path, batch_id).read_bytes() == before
    assert not list((tmp_path / "batches").glob("*.tmp.*"))


@pytest.mark.parametrize(
    "batch_id",
    [
        "../outside",
        "018f1f7e-7b4a-6c6a-8a2b-4a7f4c2e9d10",
        "018F1F7E-7B4A-7C6A-8A2B-4A7F4C2E9D10",
    ],
)
def test_batch_paths_reject_non_canonical_uuid7(tmp_path: Path, batch_id: str):
    with pytest.raises(StagingError):
        batch_file_path(tmp_path, batch_id)
    with pytest.raises(StagingError):
        batch_lock_path(tmp_path, batch_id)
    with pytest.raises(StagingError), with_batch_lock(tmp_path, batch_id):
        pass


def test_save_batch_rejects_invalid_batch_id(tmp_path: Path):
    batch = BatchFile(
        batch_id="../outside",
        target="content",
        root_path="/archive/root",
        category_id=641,
        created_at="2026-05-12T10:00:00Z",
        updated_at="2026-05-12T10:00:00Z",
    )

    with pytest.raises(StagingError, match="invalid batch_id"):
        save_batch(tmp_path, batch)

    assert not (tmp_path / "outside.json").exists()


def test_load_batch_rejects_missing_required_fields(tmp_path: Path):
    batch_id = new_batch_id()
    path = tmp_path / "batches" / f"{batch_id}.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "batch_id": batch_id,
                "target": "content",
                "root_path": "/archive/root",
                "category_id": 641,
                "created_at": "2026-05-12T10:00:00Z",
                "updated_at": "2026-05-12T10:00:00Z",
                "objects": [
                    {
                        "object_id": "obj:1",
                        "status": "prepared",
                        "digests": [],
                        "skipped_reason": None,
                    }
                ],
                "submissions": [],
                "verify_jobs": [],
            }
        )
    )

    with pytest.raises(StagingError, match="objects\\[0\\]\\.last_error"):
        load_batch(tmp_path, batch_id)


def test_load_batch_rejects_unknown_fields(tmp_path: Path):
    batch_id = new_batch_id()
    path = tmp_path / "batches" / f"{batch_id}.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "batch_id": batch_id,
                "target": "content",
                "root_path": "/archive/root",
                "category_id": 641,
                "created_at": "2026-05-12T10:00:00Z",
                "updated_at": "2026-05-12T10:00:00Z",
                "objects": [],
                "submissions": [],
                "verify_jobs": [],
                "future_field": "nope",
            }
        )
    )

    with pytest.raises(StagingError, match="invalid batch file"):
        load_batch(tmp_path, batch_id)


def test_with_batch_lock_timeout_raises(tmp_path: Path):
    batch_id = new_batch_id()
    holder_acquired = threading.Event()
    holder_release = threading.Event()

    def hold_lock():
        with with_batch_lock(tmp_path, batch_id):
            holder_acquired.set()
            holder_release.wait(timeout=5)

    thread = threading.Thread(target=hold_lock, daemon=True)
    thread.start()
    assert holder_acquired.wait(timeout=2)

    try:
        with (
            pytest.raises(LockTimeoutError),
            with_batch_lock(tmp_path, batch_id, timeout=0.1),
        ):
            pytest.fail("should have timed out")
    finally:
        holder_release.set()
        thread.join(timeout=5)


def test_with_batch_lock_serializes_writers(tmp_path: Path):
    batch_id = new_batch_id()
    barrier = threading.Barrier(2)

    def critical():
        barrier.wait(timeout=5)
        with with_batch_lock(tmp_path, batch_id, timeout=5):
            time.sleep(0.15)

    threads = [threading.Thread(target=critical, daemon=True) for _ in range(2)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    elapsed = time.monotonic() - started
    assert elapsed >= 0.3


def test_driver_lease_blocks_concurrent_driver(tmp_path: Path):
    batch_id = new_batch_id()

    with with_driver_lease(
        tmp_path,
        batch_id,
        command=("stage", "timestamp", "batch", "--upgrade"),
        heartbeat_interval=None,
    ) as lease:
        assert driver_lease_path(tmp_path, batch_id).is_file()
        assert driver_refresh_lock_path(tmp_path, batch_id).is_file()

        with (
            pytest.raises(LockTimeoutError) as excinfo,
            with_driver_lease(
                tmp_path,
                batch_id,
                command=("stage", "timestamp", "batch", "--watch"),
                heartbeat_interval=None,
            ),
        ):
            pytest.fail("second driver should not acquire a live lease")

    message = str(excinfo.value)
    assert f"batch {batch_id}" in message
    assert f"pid={lease.pid}" in message
    assert f"host={lease.host}" in message
    assert "started_at=" in message
    assert "heartbeat_at=" in message
    assert "command=stage timestamp batch --upgrade" in message
    assert not driver_lease_path(tmp_path, batch_id).exists()


def test_driver_lease_persists_spec_json_shape(tmp_path: Path):
    batch_id = new_batch_id()

    with with_driver_lease(
        tmp_path,
        batch_id,
        command=("stage", "timestamp", "batch", "--upgrade"),
        heartbeat_interval=None,
    ):
        raw = json.loads(driver_lease_path(tmp_path, batch_id).read_text())

    assert set(raw) == {
        "batch_id",
        "pid",
        "host",
        "started_at",
        "heartbeat_at",
        "command",
        "cli_version",
    }
    assert raw["batch_id"] == batch_id
    assert raw["pid"] == os.getpid()
    assert raw["host"]
    assert raw["started_at"].endswith("Z")
    assert raw["heartbeat_at"].endswith("Z")
    assert raw["command"] == "stage timestamp batch --upgrade"
    assert isinstance(raw["cli_version"], str)
    assert raw["cli_version"]


def test_driver_lease_steals_dead_local_pid(tmp_path: Path):
    batch_id = new_batch_id()
    old_started = datetime.now(UTC) - timedelta(minutes=1)
    save_driver_lease(
        tmp_path,
        batch_id,
        pid=999_999_999,
        host=os.uname().nodename,
        command=("old-driver",),
        started_at=old_started,
        heartbeat_at=old_started,
    )

    with with_driver_lease(
        tmp_path,
        batch_id,
        command=("new-driver",),
        heartbeat_interval=None,
    ) as lease:
        assert lease.pid == os.getpid()
        assert lease.command == ("new-driver",)

    stolen = sorted((tmp_path / "batches").glob(f"{batch_id}.driver.stolen.*"))
    assert len(stolen) == 1
    assert not driver_lease_path(tmp_path, batch_id).exists()


def test_driver_lease_steals_stale_heartbeat_with_live_pid(tmp_path: Path):
    batch_id = new_batch_id()
    now = datetime.now(UTC)
    save_driver_lease(
        tmp_path,
        batch_id,
        pid=os.getpid(),
        host=os.uname().nodename,
        command=("stuck-watch",),
        started_at=now - timedelta(minutes=10),
        heartbeat_at=now - timedelta(minutes=6),
    )

    with with_driver_lease(
        tmp_path,
        batch_id,
        command=("fresh-driver",),
        heartbeat_interval=None,
    ) as lease:
        assert lease.command == ("fresh-driver",)

    stolen = sorted((tmp_path / "batches").glob(f"{batch_id}.driver.stolen.*"))
    assert len(stolen) == 1


def test_driver_lease_refresh_keeps_long_running_holder_live(tmp_path: Path):
    batch_id = new_batch_id()
    now = datetime.now(UTC)

    with with_driver_lease(
        tmp_path,
        batch_id,
        command=("watch",),
        heartbeat_interval=None,
    ) as handle:
        refreshed_at = now + timedelta(minutes=4)
        handle.refresh(now=refreshed_at)

        lease = load_driver_lease(tmp_path, batch_id)
        assert lease.heartbeat_at == "Z".join(
            refreshed_at.isoformat(timespec="seconds").split("+00:00")
        )

        with (
            pytest.raises(LockTimeoutError),
            with_driver_lease(
                tmp_path,
                batch_id,
                command=("contender",),
                heartbeat_interval=None,
            ),
        ):
            pytest.fail("fresh heartbeat should not be stolen")


def test_driver_lease_steal_rechecks_under_refresh_lock(tmp_path: Path):
    batch_id = new_batch_id()
    stale = datetime.now(UTC) - timedelta(minutes=6)
    save_driver_lease(
        tmp_path,
        batch_id,
        pid=os.getpid(),
        host=os.uname().nodename,
        command=("watch",),
        started_at=stale,
        heartbeat_at=stale,
    )

    with open(driver_refresh_lock_path(tmp_path, batch_id), "w+b") as lock_file:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        result: dict[str, BaseException | None] = {"error": None}

        def steal():
            try:
                with with_driver_lease(
                    tmp_path,
                    batch_id,
                    command=("stealer",),
                    heartbeat_interval=None,
                ):
                    pass
            except BaseException as e:
                result["error"] = e

        thread = threading.Thread(target=steal, daemon=True)
        thread.start()
        time.sleep(0.05)
        fresh = datetime.now(UTC)
        save_driver_lease(
            tmp_path,
            batch_id,
            pid=os.getpid(),
            host=os.uname().nodename,
            command=("watch",),
            started_at=stale,
            heartbeat_at=fresh,
        )
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    thread.join(timeout=5)
    assert isinstance(result["error"], LockTimeoutError)
    assert load_driver_lease(tmp_path, batch_id).command == "watch"
    assert not list((tmp_path / "batches").glob(f"{batch_id}.driver.stolen.*"))


def test_driver_lease_new_holder_waits_for_refresh_lock(tmp_path: Path):
    batch_id = new_batch_id()
    lock_path = driver_refresh_lock_path(tmp_path, batch_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    acquired = threading.Event()

    with open(lock_path, "w+b") as lock_file:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

        def acquire():
            with with_driver_lease(
                tmp_path,
                batch_id,
                command=("new-driver",),
                heartbeat_interval=None,
            ):
                acquired.set()

        thread = threading.Thread(target=acquire, daemon=True)
        thread.start()
        time.sleep(0.05)
        assert not acquired.is_set()
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    thread.join(timeout=5)
    assert acquired.is_set()
    assert not driver_lease_path(tmp_path, batch_id).exists()


def test_driver_lease_acquires_from_non_main_thread(tmp_path: Path):
    batch_id = new_batch_id()
    result: dict[str, BaseException | None] = {"error": None}

    def acquire():
        try:
            with with_driver_lease(
                tmp_path,
                batch_id,
                command=("thread-driver",),
                heartbeat_interval=None,
            ):
                assert load_driver_lease(tmp_path, batch_id).command == "thread-driver"
        except BaseException as e:
            result["error"] = e

    thread = threading.Thread(target=acquire)
    thread.start()
    thread.join(timeout=5)

    assert result["error"] is None
    assert not driver_lease_path(tmp_path, batch_id).exists()


def test_driver_lease_restores_signal_handlers(tmp_path: Path):
    batch_id = new_batch_id()

    def previous_handler(signum, frame):
        raise RuntimeError("not called")

    old_handler = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, previous_handler)
    try:
        with with_driver_lease(
            tmp_path,
            batch_id,
            command=("signal-driver",),
            heartbeat_interval=None,
        ):
            assert signal.getsignal(signal.SIGTERM) != previous_handler

        assert signal.getsignal(signal.SIGTERM) == previous_handler
    finally:
        signal.signal(signal.SIGTERM, old_handler)


def test_driver_lease_signal_handler_releases_lease(tmp_path: Path):
    batch_id = new_batch_id()
    old_handler = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        with with_driver_lease(
            tmp_path,
            batch_id,
            command=("signal-driver",),
            heartbeat_interval=None,
        ):
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            cast(Callable[[int, object], None], handler)(signal.SIGTERM, None)
            assert not driver_lease_path(tmp_path, batch_id).exists()
    finally:
        signal.signal(signal.SIGTERM, old_handler)


def test_driver_lease_background_heartbeat_refreshes(tmp_path: Path):
    batch_id = new_batch_id()

    with with_driver_lease(
        tmp_path,
        batch_id,
        command=("watch",),
        heartbeat_interval=0.01,
    ) as handle:
        original = load_driver_lease(tmp_path, batch_id).heartbeat_at
        deadline = time.monotonic() + 2
        refreshed = original
        while time.monotonic() < deadline and refreshed == original:
            time.sleep(0.05)
            refreshed = load_driver_lease(tmp_path, batch_id).heartbeat_at

        assert refreshed != original
        assert handle.command == ("watch",)
