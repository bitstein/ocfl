"""Content map: staged digest -> source file."""

from dataclasses import dataclass
from typing import TypedDict


class ContentMapEntry(TypedDict):
    path: str
    size: int
    mtime: float


ContentMap = dict[str, ContentMapEntry]


@dataclass(frozen=True, slots=True)
class IngestResult:
    digest: str
    size: int
    already_existed: bool
