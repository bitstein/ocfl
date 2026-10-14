from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ObjectRow:
    """One indexed object and its head version."""

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
    """One version of an object, with its inventory metadata."""

    object_id: str
    version_num: int
    version: str
    created_at: str
    message: str | None
    user_name: str | None
    user_address: str | None


@dataclass(frozen=True, slots=True)
class PathRow:
    """A logical path in one version and the digest of its content."""

    object_id: str
    version_num: int
    logical_path: str
    digest: str


@dataclass(frozen=True, slots=True)
class BlobRow:
    """One distinct content digest; `media_type` is None until sniffed."""

    digest: str
    size: int
    media_type: str | None
    first_seen_at: str


@dataclass(frozen=True, slots=True)
class OtsAttestation:
    """A parsed `.ots` proof, keyed by the digest of its bytes.

    `block_height` is None for a pending proof. A proof that does not parse
    has `parse_error` set and an empty `attested_digest`.
    """

    proof_digest: str
    attested_digest: str
    block_height: int | None
    parsed_at: str
    parse_error: str | None


@dataclass(frozen=True, slots=True)
class ScanReport:
    """What one `Index.refresh` changed, and how long it took."""

    objects_scanned: int = 0
    objects_changed: int = 0
    versions_added: int = 0
    blobs_added: int = 0
    objects_purged: int = 0
    elapsed_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class StatusReport:
    """The index's schema version, last scans, and row counts."""

    root_path: str
    schema_version: str | None
    last_scan_at: str | None
    last_full_scan_at: str | None
    counts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class ObjectDetail:
    """An object with all its versions and its head version's paths."""

    object: ObjectRow
    versions: tuple[VersionRow, ...]
    head_paths: tuple[PathRow, ...]
