"""SimpleProof API client protocol and HTTP implementation."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from ocfl_cli._retry import retry
from ocfl_cli.errors import (
    AttestationMismatchError,
    SimpleProofAuthError,
    SimpleProofBadRequest,
    SimpleProofError,
    SimpleProofTransient,
)
from ocfl_lib.errors import OtsParseError
from ocfl_lib.ots import parse as parse_ots

if TYPE_CHECKING:
    from collections.abc import Callable


_REDACTED = "<redacted>"
_SENSITIVE_HEADERS: tuple[str, ...] = ("x-access-token", "x-api-key")


def _scrub_exc_request(exc: BaseException) -> None:
    # ``exc.request`` (property) raises when unset; read the slot.
    request = getattr(exc, "_request", None)
    headers = getattr(request, "headers", None)
    if headers is None:
        return
    for name in _SENSITIVE_HEADERS:
        if name in headers:
            headers[name] = _REDACTED


@dataclass(frozen=True, slots=True)
class SubmitResult:
    digests: tuple[str, ...]
    record_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class JobStatus:
    job_id: str
    status: str
    results_url: str | None = None


@dataclass(frozen=True, slots=True)
class SimpleProofCreds:
    """Credentials for the two SimpleProof auth schemes.

    ``access_token`` populates ``x-access-token`` for the v1 ``/hashes``
    submit endpoint. ``api_key`` populates ``x-api-key`` for the v2
    ``verify-batches`` and ``jobs/{id}`` endpoints.
    """

    access_token: str = ""
    api_key: str = ""


class SimpleProofClient(Protocol):
    def submit_hashes(self, digests: list[str], category_id: int) -> SubmitResult: ...

    def verify_batch(self, digests: list[str]) -> str: ...

    def poll_job(self, job_id: str) -> JobStatus: ...

    def download_results(
        self, results_url: str, expected_digests: set[str]
    ) -> dict[str, bytes]: ...


_SUBMIT_PATH = "/api/proof/timestamp/hashes"
_VERIFY_PATH = "/api/v2/proofs/verify-batches"
_JOBS_PATH = "/api/v2/proofs/jobs"
_CHUNK_SIZE = 1000


class SimpleProofHttpClient:
    """Real HTTP implementation.

    Callers inject a fully-configured ``httpx.Client`` so tests can swap in
    a ``MockTransport`` and lifecycle stays caller-controlled. ``sleep`` is
    forwarded to ``ocfl_cli._retry.retry`` so tests bypass wall-clock time.
    """

    def __init__(
        self,
        base_url: str,
        creds: SimpleProofCreds,
        *,
        client: httpx.Client,
        sleep: Callable[[float], None] = time.sleep,
        attempts: int = 3,
    ) -> None:
        self._base_url = base_url
        self._creds = creds
        self._client = client
        self._sleep = sleep
        self._attempts = attempts

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
    ) -> httpx.Response:
        try:
            resp = self._client.request(method, url, headers=headers, json=json_body)
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as exc:
            _scrub_exc_request(exc)
            status = exc.response.status_code
            if status in (401, 403):
                raise SimpleProofAuthError(
                    f"SimpleProof auth failed: HTTP {status}"
                ) from exc
            if status in (400, 422):
                raise SimpleProofBadRequest(
                    f"SimpleProof rejected request: HTTP {status}"
                ) from exc
            if status == 429:
                transient = SimpleProofTransient(
                    f"SimpleProof rate-limited: HTTP {status}"
                )
                retry_after = exc.response.headers.get("Retry-After")
                if retry_after is not None:
                    transient.retry_after = float(retry_after)
                raise transient from exc
            if 500 <= status < 600:
                raise SimpleProofTransient(f"SimpleProof HTTP {status}") from exc
            raise SimpleProofError(f"SimpleProof HTTP {status}") from exc
        except (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.NetworkError,
        ) as exc:
            _scrub_exc_request(exc)
            raise SimpleProofTransient(
                f"SimpleProof transport error: {type(exc).__name__}"
            ) from exc

    def submit_hashes(self, digests: list[str], category_id: int) -> SubmitResult:
        all_digests: list[str] = []
        all_record_ids: list[str] = []
        chunks = [
            digests[i : i + _CHUNK_SIZE] for i in range(0, len(digests), _CHUNK_SIZE)
        ]
        for chunk in chunks:
            try:
                records = retry(
                    lambda c=chunk: self._submit_chunk(c, category_id),
                    sleep=self._sleep,
                    attempts=self._attempts,
                )
            except SimpleProofError as exc:
                if all_digests:
                    exc.partial_result = SubmitResult(
                        digests=tuple(all_digests),
                        record_ids=tuple(all_record_ids),
                    )
                raise
            all_digests.extend(chunk)
            all_record_ids.extend(str(rec["recordId"]) for rec in records)
        return SubmitResult(
            digests=tuple(all_digests),
            record_ids=tuple(all_record_ids),
        )

    def _submit_chunk(
        self, hashes: list[str], category_id: int
    ) -> list[dict[str, Any]]:
        resp = self._request(
            "POST",
            self._base_url + _SUBMIT_PATH,
            headers={"x-access-token": self._creds.access_token},
            json_body={"proofCategoryId": category_id, "hashes": hashes},
        )
        return list(resp.json()["records"])

    def verify_batch(self, digests: list[str]) -> str:
        resp = retry(
            lambda: self._request(
                "POST",
                self._base_url + _VERIFY_PATH,
                headers={"x-api-key": self._creds.api_key},
                json_body={"hashes": digests},
            ),
            sleep=self._sleep,
            attempts=self._attempts,
        )
        return str(resp.json()["jobId"])

    def poll_job(self, job_id: str) -> JobStatus:
        resp = retry(
            lambda: self._request(
                "GET",
                f"{self._base_url}{_JOBS_PATH}/{job_id}",
                headers={"x-api-key": self._creds.api_key},
            ),
            sleep=self._sleep,
            attempts=self._attempts,
        )
        body = resp.json()
        results_url = body.get("resultsUrl")
        return JobStatus(
            job_id=str(body["jobId"]),
            status=str(body["status"]),
            results_url=str(results_url) if results_url is not None else None,
        )

    def download_results(
        self, results_url: str, expected_digests: set[str]
    ) -> dict[str, bytes]:
        del expected_digests
        resp = retry(
            lambda: self._request("GET", results_url),
            sleep=self._sleep,
            attempts=self._attempts,
        )
        out: dict[str, bytes] = {}
        for entry in resp.json()["results"]:
            claimed = str(entry["digest"])
            ots_url = str(entry["otsDownloadUrl"])
            ots_resp = retry(
                lambda u=ots_url: self._request("GET", u),
                sleep=self._sleep,
                attempts=self._attempts,
            )
            ots_bytes = ots_resp.content
            try:
                parsed = parse_ots(ots_bytes)
            except OtsParseError as exc:
                raise AttestationMismatchError(
                    f"unparseable .ots bytes for digest {claimed}"
                ) from exc
            parsed_hex = parsed.commitment_digest.hex()
            if parsed_hex != claimed:
                raise AttestationMismatchError(
                    f"commitment digest mismatch: claimed={claimed} parsed={parsed_hex}"
                )
            out[claimed] = ots_bytes
        return out
