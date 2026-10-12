"""Fakes for the time ports: a settable clock and a sleeper that only records."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta


@dataclass
class FakeClock:
    """`Clock` that returns `current` until a test moves it."""

    current: datetime = field(default_factory=lambda: datetime(2030, 1, 2, tzinfo=UTC))

    def now(self) -> datetime:
        return self.current

    def set(self, value: datetime) -> None:
        self.current = value

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


@dataclass
class RecordingSleeper:
    """`Sleeper` that records each requested sleep and returns at once."""

    calls: list[float] = field(default_factory=list)

    def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)
