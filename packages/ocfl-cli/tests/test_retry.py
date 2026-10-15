from collections.abc import Callable

import pytest

from ocfl_cli.errors import Transient
from ocfl_cli.ots._retry import retry


class _FatalError(Exception):
    pass


def _make_sleep() -> tuple[list[float], Callable[[float], None]]:
    calls: list[float] = []
    return calls, calls.append


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
                raise Transient("boom")
            return 42

        result = retry(
            fn,
            attempts=3,
            backoff=(2.0, 4.0),
            sleep=sleep,
        )

        assert result == 42
        assert calls == 3
        assert sleeps == [2.0, 4.0]

    def test_retries_exhausted_reraises_last(self):
        sleeps, sleep = _make_sleep()
        calls = 0

        def fn() -> None:
            nonlocal calls
            calls += 1
            raise Transient(f"fail-{calls}")

        with pytest.raises(Transient, match="fail-3"):
            retry(fn, attempts=3, backoff=(2.0, 4.0), sleep=sleep)

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
            retry(fn, attempts=3, backoff=(2.0, 4.0), sleep=sleep)

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
                raise Transient("429", retry_after=7.5)
            return "ok"

        result = retry(fn, attempts=3, backoff=(2.0, 4.0), sleep=sleep)

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
                raise Transient("first")
            if calls == 2:
                raise Transient("429", retry_after=9.0)
            return "done"

        result = retry(fn, attempts=3, backoff=(2.0, 4.0), sleep=sleep)

        assert result == "done"
        assert calls == 3
        assert sleeps == [2.0, 9.0]


class TestAttemptsOne:
    def test_attempts_one_runs_once_no_sleep(self):
        sleeps, sleep = _make_sleep()
        calls = 0

        def fn() -> str:
            nonlocal calls
            calls += 1
            return "single"

        result = retry(fn, attempts=1, backoff=(2.0, 4.0), sleep=sleep)

        assert result == "single"
        assert calls == 1
        assert sleeps == []

    def test_attempts_one_transient_no_retry(self):
        sleeps, sleep = _make_sleep()
        calls = 0

        def fn() -> None:
            nonlocal calls
            calls += 1
            raise Transient("once")

        with pytest.raises(Transient):
            retry(fn, attempts=1, backoff=(2.0, 4.0), sleep=sleep)

        assert calls == 1
        assert sleeps == []


class TestBackoffSchedule:
    def test_default_backoff_is_exponential_and_capped(self):
        sleeps, sleep = _make_sleep()
        calls = 0

        def fn() -> int:
            nonlocal calls
            calls += 1
            raise Transient("x")

        with pytest.raises(Transient):
            retry(fn, attempts=8, sleep=sleep)

        assert sleeps == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
