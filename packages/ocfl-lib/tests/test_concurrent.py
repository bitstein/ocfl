"""Tests for bounded_map."""

import math
import threading
import time

import pytest

from ocfl_lib._concurrent import bounded_map


class TestBoundedMap:
    def test_basic_results(self):
        results = list(bounded_map(math.sqrt, range(1, 101), max_workers=4))
        assert results == [math.sqrt(i) for i in range(1, 101)]

    def test_empty_iterable(self):
        assert list(bounded_map(str, [], max_workers=2)) == []

    def test_fail_fast(self):
        called = set()

        def fn(x: int) -> int:
            called.add(x)
            if x == 5:
                raise ValueError("boom")
            time.sleep(0.01)
            return x

        with pytest.raises(ValueError, match="boom"):
            list(bounded_map(fn, range(100), max_workers=2, max_outstanding=4))

        # Items well beyond the window should never be called
        assert max(called) < 20

    def test_bounded_memory(self):
        """Peak in-flight futures stay within window size."""
        peak = [0]
        current = [0]
        lock = threading.Lock()

        def fn(x: int) -> int:
            with lock:
                current[0] += 1
                if current[0] > peak[0]:
                    peak[0] = current[0]
            time.sleep(0.001)
            with lock:
                current[0] -= 1
            return x

        workers = 4
        cap = 2 * workers
        list(bounded_map(fn, range(200), max_workers=workers, max_outstanding=cap))
        # Peak should be at most max_outstanding + small margin
        assert peak[0] <= 2 * workers + workers

    def test_consumer_abandon(self):
        call_count = [0]

        def fn(x: int) -> int:
            call_count[0] += 1
            time.sleep(0.01)
            return x

        gen = bounded_map(fn, range(10000), max_workers=2, max_outstanding=4)
        next(gen)
        gen.close()

        # Should not have processed all 10k items
        assert call_count[0] < 100

    def test_preserves_order(self):
        """Results come back in submission order even with varying latency."""

        def fn(x: int) -> int:
            time.sleep(0.001 * (x % 3))
            return x

        results = list(bounded_map(fn, range(50), max_workers=4))
        assert results == list(range(50))
