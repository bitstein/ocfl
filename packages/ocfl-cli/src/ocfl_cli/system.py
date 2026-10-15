import time
from datetime import UTC, datetime


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SystemSleeper:
    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


SYSTEM_CLOCK = SystemClock()
SYSTEM_SLEEPER = SystemSleeper()
