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
        fail_at = 5
        cap = 4

        def fn(x: int) -> int:
            called.add(x)
            if x == fail_at:
                raise ValueError("boom")
            time.sleep(0.01)
            return x

        with pytest.raises(ValueError, match="boom"):
            list(bounded_map(fn, range(100), max_workers=2, max_outstanding=cap))

        assert max(called) < fail_at + cap

    def test_peak_in_flight_stays_within_outstanding_cap(self):
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
        assert peak[0] <= cap

    def test_submission_does_not_exceed_outstanding_cap_before_yield(self):
        release = threading.Event()
        pulled = [0]
        lock = threading.Lock()

        class CountingIterable:
            def __iter__(self):
                for i in range(100):
                    with lock:
                        pulled[0] += 1
                    yield i

        def fn(x: int) -> int:
            release.wait(timeout=5)
            return x

        gen = bounded_map(fn, CountingIterable(), max_workers=2, max_outstanding=4)
        result: list[int] = []

        def consume_one() -> None:
            result.append(next(gen))

        thread = threading.Thread(target=consume_one)
        thread.start()

        try:
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                with lock:
                    if pulled[0] >= 4:
                        break
                time.sleep(0.001)

            time.sleep(0.05)
            with lock:
                assert pulled[0] == 4

        finally:
            release.set()
            thread.join(timeout=1)
            gen.close()
        assert result == [0]

    def test_consumer_abandon(self):
        call_count = [0]

        def fn(x: int) -> int:
            call_count[0] += 1
            time.sleep(0.01)
            return x

        cap = 4
        gen = bounded_map(fn, range(10000), max_workers=2, max_outstanding=cap)
        next(gen)
        gen.close()

        assert call_count[0] <= cap + 1

    def test_preserves_order(self):
        def fn(x: int) -> int:
            time.sleep(0.001 * (x % 3))
            return x

        results = list(bounded_map(fn, range(50), max_workers=4))
        assert results == list(range(50))
