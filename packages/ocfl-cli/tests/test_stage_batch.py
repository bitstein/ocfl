import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
from fakes import RecordingSleeper
from fakes.locks import held_flock, is_flocked, lock_runtime

from ocfl_cli.errors import LockTimeoutError, StagingError
from ocfl_cli.model.batch import BatchFile, BatchObject, BatchSubmission, BatchVerifyJob
from ocfl_cli.model.phase import TimestampTarget
from ocfl_cli.store.batch import (
    batch_file_path,
    batch_lock,
    batch_lock_path,
    load_batch,
    new_batch_id,
    save_batch,
)


def test_batch_file_round_trips_required_schema(tmp_path: Path):
    batch_id = new_batch_id()
    batch = BatchFile(
        batch_id=batch_id,
        target=TimestampTarget.CONTENT,
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
        target=TimestampTarget.CONTENT,
        root_path="/archive/root",
        category_id=641,
        created_at="2026-05-12T10:00:00Z",
        updated_at="2026-05-12T10:00:00Z",
    )
    replacement = BatchFile(
        batch_id=batch_id,
        target=TimestampTarget.CONTENT,
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

    monkeypatch.setattr(os, "replace", fail_replace)

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
    with pytest.raises(StagingError), batch_lock(tmp_path, batch_id, lock_runtime()):
        pass


def test_save_batch_rejects_invalid_batch_id(tmp_path: Path):
    batch = BatchFile(
        batch_id="../outside",
        target=TimestampTarget.CONTENT,
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


def test_batch_lock_timeout_raises(tmp_path: Path):
    batch_id = new_batch_id()
    holder_acquired = threading.Event()
    holder_release = threading.Event()

    def hold_lock():
        with batch_lock(tmp_path, batch_id, lock_runtime()):
            holder_acquired.set()
            holder_release.wait(timeout=5)

    thread = threading.Thread(target=hold_lock, daemon=True)
    thread.start()
    assert holder_acquired.wait(timeout=2)

    try:
        with (
            pytest.raises(LockTimeoutError, match=f"batch {batch_id}"),
            batch_lock(tmp_path, batch_id, lock_runtime(0.1)),
        ):
            pytest.fail("should have timed out")
    finally:
        holder_release.set()
        thread.join(timeout=5)


def test_batch_lock_serializes_writers(tmp_path: Path):
    batch_id = new_batch_id()
    barrier = threading.Barrier(2)

    def critical():
        barrier.wait(timeout=5)
        with batch_lock(tmp_path, batch_id, lock_runtime(5)):
            time.sleep(0.15)

    threads = [threading.Thread(target=critical, daemon=True) for _ in range(2)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    elapsed = time.monotonic() - started
    assert elapsed >= 0.3


def test_batch_lock_waits_through_the_sleeper(tmp_path: Path):
    batch_id = new_batch_id()
    sleeper = RecordingSleeper()
    with held_flock(batch_lock_path(tmp_path, batch_id)):
        started = time.monotonic()
        with (
            pytest.raises(LockTimeoutError),
            batch_lock(tmp_path, batch_id, lock_runtime(5, sleeper=sleeper)),
        ):
            pytest.fail("should have timed out")
        assert time.monotonic() - started < 2
    assert sum(sleeper.calls) == pytest.approx(5.0)
    assert len(sleeper.calls) == 50


def test_batch_lock_is_free_once_its_holder_dies(tmp_path: Path):
    batch_id = new_batch_id()
    path = batch_lock_path(tmp_path, batch_id)
    path.parent.mkdir(parents=True)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl,sys,time\n"
            "f=open(sys.argv[1],'a+b')\n"
            "fcntl.flock(f.fileno(),fcntl.LOCK_EX)\n"
            "print('held',flush=True)\n"
            "time.sleep(60)\n",
            str(path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        assert is_flocked(path)
        os.kill(holder.pid, signal.SIGKILL)
        holder.wait(timeout=10)
    finally:
        if holder.poll() is None:
            holder.kill()
    with batch_lock(tmp_path, batch_id, lock_runtime(0)):
        assert is_flocked(path)
    assert not is_flocked(path)
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]
