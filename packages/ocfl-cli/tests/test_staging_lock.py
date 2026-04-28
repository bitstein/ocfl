"""filelock integration: with_lock blocks writers; load_ots is lockless.

Pinned behavior:
- with_lock(staging) wraps filelock.FileLock("<staging>/.lock").
- Two concurrent acquires: second blocks; timeout=0.1 raises LockTimeoutError.
- load_ots does NOT acquire the lock — `stage status` reads the
  atomically-rewritten ots.json directly. Bounded staleness is correct.
"""

import json
import threading
import time
from pathlib import Path

import pytest

from ocfl_cli.errors import LockTimeoutError
from ocfl_cli.stage.state import load_ots, with_lock


@pytest.fixture
def staging(tmp_path: Path) -> Path:
    return tmp_path


def test_with_lock_creates_lockfile(staging: Path):
    with with_lock(staging):
        assert (staging / ".lock").exists()


def test_with_lock_timeout_raises(staging: Path):
    holder_acquired = threading.Event()
    holder_release = threading.Event()
    holder_done = threading.Event()

    def hold():
        with with_lock(staging):
            holder_acquired.set()
            holder_release.wait(timeout=5)
        holder_done.set()

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert holder_acquired.wait(timeout=2)

    try:
        with (
            pytest.raises(LockTimeoutError),
            with_lock(staging, timeout=0.1),
        ):
            pytest.fail("should have timed out")
    finally:
        holder_release.set()
        t.join(timeout=5)
        assert holder_done.is_set()


def test_with_lock_serializes_concurrent_writers(staging: Path):
    """Two threads, each holds the lock for ~150ms; total wall time >= 300ms."""
    barrier = threading.Barrier(2)

    def critical():
        barrier.wait(timeout=5)
        with with_lock(staging, timeout=5):
            time.sleep(0.15)

    threads = [threading.Thread(target=critical, daemon=True) for _ in range(2)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.3, f"expected serialization, got {elapsed:.3f}s"


def test_with_lock_releases_on_exception(staging: Path):
    with pytest.raises(RuntimeError, match="boom"), with_lock(staging):
        raise RuntimeError("boom")
    # Re-acquire immediately — would block/timeout if not released.
    with with_lock(staging, timeout=0.5):
        pass


def test_load_ots_does_not_acquire_lock(staging: Path):
    """`stage status` is read-only and must never block on the writer lock."""
    (staging / "ots.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "phase": "drafting",
                "category_id": 641,
                "pending_digests": [],
                "submissions": [],
                "verify_jobs": [],
                "content_proofs": {},
                "inventory_proof": None,
            }
        )
    )

    holder_acquired = threading.Event()
    holder_release = threading.Event()

    def hold():
        with with_lock(staging):
            holder_acquired.set()
            holder_release.wait(timeout=5)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    try:
        assert holder_acquired.wait(timeout=2)
        # Lock is held by the other thread. load_ots must still succeed quickly.
        t0 = time.monotonic()
        state = load_ots(staging)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"load_ots blocked on lock ({elapsed:.3f}s)"
        assert state.category_id == 641
    finally:
        holder_release.set()
        t.join(timeout=5)
