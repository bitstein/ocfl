"""Pure helper to digest a source file and record it in a content map."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict

from ocfl_lib import file_digest

if TYPE_CHECKING:
    from pathlib import Path

    from ocfl_lib import DigestAlgorithm


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


def ingest_file(
    src: Path,
    obj_rel_path: str,
    *,
    staging_dir: Path,
    content_map: ContentMap,
    digest_alg: DigestAlgorithm,
) -> IngestResult:
    digest = file_digest(src, digest_alg)
    stat = src.stat()
    already_existed = digest in content_map
    if not already_existed:
        content_map[digest] = {
            "path": str(src.resolve()),
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        }
    return IngestResult(
        digest=digest, size=stat.st_size, already_existed=already_existed
    )
