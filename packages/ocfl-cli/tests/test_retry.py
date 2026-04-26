"""Tests for ocfl_cli.ots._retry.retry helper."""

from collections.abc import Callable

import pytest

from ocfl_cli.errors import Transient
from ocfl_cli.ots._retry import retry


class _FakeTransient(Exception, Transient):
    pass


class _FatalError(Exception):
    pass


def _make_sleep() -> tuple[list[float], Callable[[float], None]]:
    calls: list[float] = []
    return calls, calls.append


def _within_jitter(actual: float, expected: float, pct: float = 0.25) -> bool:
    lo = expected * (1.0 - pct)
    hi = expected * (1.0 + pct)
    return lo <= actual <= hi


class TestSuccessPath:
    def test_success_first_attempt_no_sleep(self):
        sleeps, sleep = _make_sleep()
        calls = 0

        def fn() -> str:
            nonlocal calls
            calls += 1
            return "ok"

        result = retry(fn, sleep=sleep)

        assert result == "ok"
        assert calls == 1
        assert sleeps == []


class TestRetryOnTransient:
    def test_two_transients_then_success(self):
        sleeps, sleep = _make_sleep()
        calls = 0

        def fn() -> int:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise _FakeTransient("boom")
            return 42

        result = retry(
            fn,
            attempts=3,
            backoff=(1.0, 2.0),
            sleep=sleep,
        )

        assert result == 42
        assert calls == 3
        assert len(sleeps) == 2
        assert _within_jitter(sleeps[0], 1.0)
        assert _within_jitter(sleeps[1], 2.0)

    def test_retries_exhausted_reraises_last(self):
        sleeps, sleep = _make_sleep()
        calls = 0

        def fn() -> None:
            nonlocal calls
            calls += 1
            raise _FakeTransient(f"fail-{calls}")

        with pytest.raises(_FakeTransient, match="fail-3"):
            retry(fn, attempts=3, backoff=(1.0, 2.0), sleep=sleep)

        assert calls == 3
        assert len(sleeps) == 2


class TestFailFast:
    def test_non_transient_propagates_immediately(self):
        sleeps, sleep = _make_sleep()
        calls = 0

        def fn() -> None:
            nonlocal calls
            calls += 1
            raise _FatalError("nope")

        with pytest.raises(_FatalError, match="nope"):
            retry(fn, attempts=3, backoff=(1.0, 2.0), sleep=sleep)

        assert calls == 1
        assert sleeps == []


class TestRetryAfterOverride:
    def test_retry_after_overrides_backoff_exact(self):
        sleeps, sleep = _make_sleep()
        calls = 0

        def fn() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                exc = _FakeTransient("429")
                exc.retry_after = 7.5
                raise exc
            return "ok"

        result = retry(fn, attempts=3, backoff=(1.0, 2.0), sleep=sleep)

        assert result == "ok"
        assert calls == 2
        assert sleeps == [7.5]

    def test_mixed_backoff_then_retry_after(self):
        sleeps, sleep = _make_sleep()
        calls = 0

        def fn() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise _FakeTransient("first")
            if calls == 2:
                exc = _FakeTransient("429")
                exc.retry_after = 9.0
                raise exc
            return "done"

        result = retry(fn, attempts=3, backoff=(1.0, 2.0), sleep=sleep)

        assert result == "done"
        assert calls == 3
        assert len(sleeps) == 2
        assert _within_jitter(sleeps[0], 1.0)
        assert sleeps[1] == 9.0


class TestAttemptsOne:
    def test_attempts_one_runs_once_no_sleep(self):
        sleeps, sleep = _make_sleep()
        calls = 0

        def fn() -> str:
            nonlocal calls
            calls += 1
            return "single"

        result = retry(fn, attempts=1, backoff=(1.0, 2.0), sleep=sleep)

        assert result == "single"
        assert calls == 1
        assert sleeps == []

    def test_attempts_one_transient_no_retry(self):
        sleeps, sleep = _make_sleep()
        calls = 0

        def fn() -> None:
            nonlocal calls
            calls += 1
            raise _FakeTransient("once")

        with pytest.raises(_FakeTransient):
            retry(fn, attempts=1, backoff=(1.0, 2.0), sleep=sleep)

        assert calls == 1
        assert sleeps == []


class TestJitterBounds:
    def test_jitter_stays_within_band_across_runs(self):
        samples_slot0: list[float] = []
        samples_slot1: list[float] = []

        for _ in range(50):
            sleeps, sleep = _make_sleep()
            calls = 0

            def fn() -> int:
                nonlocal calls
                calls += 1
                if calls < 3:
                    raise _FakeTransient("x")
                return 1

            retry(fn, attempts=3, backoff=(1.0, 2.0), sleep=sleep)
            assert len(sleeps) == 2
            samples_slot0.append(sleeps[0])
            samples_slot1.append(sleeps[1])

        assert all(0.75 <= s <= 1.25 for s in samples_slot0)
        assert all(1.50 <= s <= 2.50 for s in samples_slot1)
