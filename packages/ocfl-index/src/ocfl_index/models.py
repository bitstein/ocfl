"""Frozen dataclasses returned by Index queries and refresh."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class ObjectRow:
    object_id: str
    root_relpath: str
    spec_version: str
    digest_algorithm: str
    head_version: str
    head_version_num: int
    head_created_at: str
    inventory_digest: str
    indexed_at: str


@dataclass(frozen=True, slots=True)
class VersionRow:
    object_id: str
    version_num: int
    version: str
    created_at: str
    message: str | None
    user_name: str | None
    user_address: str | None


@dataclass(frozen=True, slots=True)
class PathRow:
    object_id: str
    version_num: int
    logical_path: str
    digest: str


@dataclass(frozen=True, slots=True)
class BlobRow:
    digest: str
    size: int
    media_type: str | None
    first_seen_at: str


@dataclass(frozen=True, slots=True)
class OtsAttestation:
    proof_digest: str
    attested_digest: str
    block_height: int | None
    parsed_at: str
    parse_error: str | None


@dataclass(frozen=True, slots=True)
class ScanReport:
    objects_scanned: int = 0
    objects_changed: int = 0
    versions_added: int = 0
    blobs_added: int = 0
    objects_purged: int = 0
    elapsed_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class StatusReport:
    root_path: str
    schema_version: str | None
    last_scan_at: str | None
    last_full_scan_at: str | None
    counts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class ObjectDetail:
    object: ObjectRow
    versions: tuple[VersionRow, ...]
    head_paths: tuple[PathRow, ...]
