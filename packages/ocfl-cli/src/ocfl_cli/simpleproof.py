"""SimpleProof API client protocol and skeleton implementation."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class SubmitResult:
    digests: tuple[str, ...]
    record_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class JobStatus:
    job_id: str
    status: str
    results_url: str | None = None


@runtime_checkable
class SimpleProofClient(Protocol):
    def submit_hashes(self, digests: list[str], category_id: int) -> SubmitResult: ...

    def verify_batch(self, digests: list[str]) -> str: ...

    def poll_job(self, job_id: str) -> JobStatus: ...

    def download_results(
        self, results_url: str, expected_digests: set[str]
    ) -> dict[str, bytes]: ...


class SimpleProofHttpClient:
    """Real HTTP implementation."""

    def submit_hashes(self, digests: list[str], category_id: int) -> SubmitResult:
        raise NotImplementedError

    def verify_batch(self, digests: list[str]) -> str:
        raise NotImplementedError

    def poll_job(self, job_id: str) -> JobStatus:
        raise NotImplementedError

    def download_results(
        self, results_url: str, expected_digests: set[str]
    ) -> dict[str, bytes]:
        raise NotImplementedError
