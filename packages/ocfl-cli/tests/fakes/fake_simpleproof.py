"""In-memory FakeSimpleProof mirroring the `SimpleProofClient` protocol."""

from dataclasses import dataclass, field

from ocfl_cli.ots.simpleproof import (
    CalendarAttestation,
    JobStatus,
    ResultsEntry,
    SimpleProofDownloadResult,
    SubmitResult,
    VerifyBatchAck,
)


@dataclass
class _Submission:
    digests: tuple[str, ...]
    category_id: int
    record_ids: tuple[str, ...]


@dataclass
class _Job:
    job_id: str
    digests: tuple[str, ...]
    status: str = "IN_PROGRESS"
    results_url: str | None = None
    total_hashes: int | None = None
    verified_hashes: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class FakeSimpleProof:
    fail_4xx: bool = False
    fail_5xx: bool = False
    fail_timeout: bool = False
    truncate_downloads: bool = False
    retry_after_seconds: float | None = None

    _submissions: list[_Submission] = field(default_factory=list)
    _jobs: dict[str, _Job] = field(default_factory=dict)
    _results: dict[str, dict[str, bytes]] = field(default_factory=dict)
    _entries: dict[str, tuple[ResultsEntry, ...]] = field(default_factory=dict)
    _next_record: int = 0
    _next_job: int = 0

    def submit_hashes(self, digests: list[str], category_id: int) -> SubmitResult:
        record_ids = tuple(f"rec-{self._alloc_record()}" for _ in digests)
        self._submissions.append(
            _Submission(
                digests=tuple(digests),
                category_id=category_id,
                record_ids=record_ids,
            )
        )
        return SubmitResult(digests=tuple(digests), record_ids=record_ids)

    def verify_batch(self, digests: list[str], category_id: int = 0) -> VerifyBatchAck:
        job_id = f"job-{self._alloc_job()}"
        self._jobs[job_id] = _Job(
            job_id=job_id,
            digests=tuple(digests),
            total_hashes=len(digests),
            verified_hashes=0,
        )
        return VerifyBatchAck(
            job_id=job_id,
            submitted_hashes=None,
            total_documents_found=None,
        )

    def poll_job(self, job_id: str) -> JobStatus:
        job = self._jobs[job_id]
        return JobStatus(
            job_id=job.job_id,
            status=job.status,
            results_url=job.results_url,
            total_hashes=job.total_hashes,
            verified_hashes=job.verified_hashes,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )

    def fetch_results_entries(self, results_url: str) -> tuple[ResultsEntry, ...]:
        return self._entries.get(results_url, ())

    def download_results(
        self, results_url: str, expected_digests: set[str]
    ) -> SimpleProofDownloadResult:
        proofs = dict(self._results.get(results_url, {}))
        return SimpleProofDownloadResult(
            proofs=proofs,
            missing_digests=frozenset(expected_digests - set(proofs)),
        )

    def advance_to_completed(
        self, job_id: str, results: dict[str, bytes] | None = None
    ) -> None:
        job = self._jobs[job_id]
        url = f"results://{job_id}"
        job.status = "COMPLETED"
        job.results_url = url
        job.verified_hashes = len(results or job.digests)
        self._results[url] = dict(results or {})
        self._entries[url] = tuple(
            ResultsEntry(
                digest=digest,
                confirmed=True,
                attestations=(),
                ots_download_url=f"ots://{digest}",
            )
            for digest in self._results[url]
        )

    def advance_to_processing(
        self,
        job_id: str,
        results: dict[str, tuple[CalendarAttestation, ...]],
        *,
        updated_at: str | None = None,
    ) -> None:
        job = self._jobs[job_id]
        url = f"results://{job_id}"
        job.status = "PROCESSING"
        job.results_url = url
        job.verified_hashes = len(results)
        job.updated_at = updated_at
        self._entries[url] = tuple(
            ResultsEntry(
                digest=digest,
                confirmed=bool(attestations),
                attestations=attestations,
                ots_download_url=f"ots://{digest}",
            )
            for digest, attestations in results.items()
        )

    def fail_job(self, job_id: str) -> None:
        self._jobs[job_id].status = "FAILED"

    def _alloc_record(self) -> int:
        self._next_record += 1
        return self._next_record

    def _alloc_job(self) -> int:
        self._next_job += 1
        return self._next_job
