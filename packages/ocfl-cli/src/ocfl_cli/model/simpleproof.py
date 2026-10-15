from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class SubmitResult:
    digests: tuple[str, ...]
    record_ids: tuple[str, ...]


class VerifyJobStatus(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


ACTIVE_VERIFY_STATUSES = frozenset(
    {VerifyJobStatus.PENDING, VerifyJobStatus.IN_PROGRESS, VerifyJobStatus.PROCESSING}
)
RESUBMIT_VERIFY_STATUSES = frozenset({VerifyJobStatus.FAILED, VerifyJobStatus.EXPIRED})
TERMINAL_VERIFY_STATUSES = RESUBMIT_VERIFY_STATUSES | {VerifyJobStatus.COMPLETED}


@dataclass(frozen=True, slots=True)
class JobStatus:
    job_id: str
    status: str
    results_url: str | None = None
    total_hashes: int | None = None
    verified_hashes: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class CalendarAttestation:
    calendar: str
    block_height: int
    transaction: str
    merkle_root: str


@dataclass(frozen=True, slots=True)
class ResultsEntry:
    digest: str
    confirmed: bool
    attestations: tuple[CalendarAttestation, ...]
    ots_download_url: str


@dataclass(frozen=True, slots=True)
class VerifyBatchAck:
    job_id: str
    submitted_hashes: int | None = None
    total_documents_found: int | None = None


@dataclass(frozen=True, slots=True)
class SimpleProofDownloadResult(Mapping[str, bytes]):
    proofs: Mapping[str, bytes]
    missing_digests: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "proofs", MappingProxyType(dict(self.proofs)))

    def __getitem__(self, digest: str) -> bytes:
        return self.proofs[digest]

    def __iter__(self) -> Iterator[str]:
        return iter(self.proofs)

    def __len__(self) -> int:
        return len(self.proofs)
