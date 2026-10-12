"""System adapters for the time ports: the real clock and real sleep.

The one module in ``ocfl_cli`` that reads the time or sleeps; everything
else gets a ``Clock`` / ``Sleeper`` through ``AppCtx`` / ``Runtime``.
"""

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
