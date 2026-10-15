from dataclasses import dataclass, field
from typing import Any


@dataclass
class RecordingBatchEvents:
    events: list[tuple[str, Any]] = field(default_factory=list)
    commit_events: list[dict[str, Any]] = field(default_factory=list)

    def created(self, batch) -> None:
        self.events.append(("created", batch))

    def resumed(self, batch) -> None:
        self.events.append(("resumed", batch))

    def object_prepared(self, batch, row) -> None:
        self.events.append(("object_prepared", row))

    def changed(self, before, after) -> None:
        self.events.append(("changed", after))

    def waiting(self, batch) -> None:
        self.events.append(("waiting", batch))

    def block(self, before, after, *, height, block_hash, reverified) -> None:
        self.events.append(("block", height))

    def watch_done(self, batch) -> None:
        self.events.append(("watch_done", batch))

    def commit_event(self, data) -> None:
        self.events.append(("commit_event", data))
        self.commit_events.append(dict(data))
