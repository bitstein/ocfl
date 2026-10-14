from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta


@dataclass
class FakeClock:
    current: datetime = field(default_factory=lambda: datetime(2030, 1, 2, tzinfo=UTC))

    def now(self) -> datetime:
        return self.current

    def set(self, value: datetime) -> None:
        self.current = value

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


@dataclass
class RecordingSleeper:
    calls: list[float] = field(default_factory=list)

    def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)
