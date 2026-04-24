"""In-memory FakeSimpleProof mirroring the `SimpleProofClient` protocol."""

from dataclasses import dataclass, field

from ocfl_cli.simpleproof import JobStatus, SubmitResult


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

    def verify_batch(self, digests: list[str]) -> str:
        job_id = f"job-{self._alloc_job()}"
        self._jobs[job_id] = _Job(job_id=job_id, digests=tuple(digests))
        return job_id

    def poll_job(self, job_id: str) -> JobStatus:
        job = self._jobs[job_id]
        return JobStatus(
            job_id=job.job_id,
            status=job.status,
            results_url=job.results_url,
        )

    def download_results(
        self, results_url: str, expected_digests: set[str]
    ) -> dict[str, bytes]:
        return dict(self._results.get(results_url, {}))

    def advance_to_completed(
        self, job_id: str, results: dict[str, bytes] | None = None
    ) -> None:
        job = self._jobs[job_id]
        url = f"results://{job_id}"
        job.status = "COMPLETED"
        job.results_url = url
        self._results[url] = dict(results or {})

    def fail_job(self, job_id: str) -> None:
        self._jobs[job_id].status = "FAILED"

    def _alloc_record(self) -> int:
        self._next_record += 1
        return self._next_record

    def _alloc_job(self) -> int:
        self._next_job += 1
        return self._next_job
