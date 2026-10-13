"""SimpleProof API client protocol and HTTP implementation."""

from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

import httpx2 as httpx

from ocfl_cli.errors import (
    AttestationMismatchError,
    SimpleProofAuthError,
    SimpleProofBadRequest,
    SimpleProofError,
    SimpleProofTransient,
)
from ocfl_cli.model.simpleproof import (
    CalendarAttestation,
    JobStatus,
    ResultsEntry,
    SimpleProofDownloadResult,
    SubmitResult,
    VerifyBatchAck,
)
from ocfl_cli.ots._retry import retry
from ocfl_cli.system import SYSTEM_CLOCK, SYSTEM_SLEEPER
from ocfl_lib import OtsParseError
from ocfl_lib.ots import parse as parse_ots

if TYPE_CHECKING:
    from collections.abc import Callable

    from ocfl_cli.config import Config
    from ocfl_cli.ports import Clock, SimpleProofClient


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
class SimpleProofCreds:
    """Credentials for the two SimpleProof auth schemes.

    ``access_token`` populates ``x-access-token`` for the v1 ``/hashes``
    submit endpoint. ``api_key`` populates ``x-api-key`` for the v2
    ``verify-batches`` and ``jobs/{id}`` endpoints.
    """

    access_token: str = ""
    api_key: str = ""


_SUBMIT_PATH = "/api/proof/timestamp/hashes"
_VERIFY_PATH = "/api/v2/proofs/verify-batches"
_JOBS_PATH = "/api/v2/proofs/jobs"
_CATEGORIES_PATH = "/api/entity/proofCategories"
SUBMIT_CHUNK_SIZE = 1000
VERIFY_BATCH_CHUNK_SIZE = 1000


def _json_object(resp: httpx.Response, context: str) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError as exc:
        raise SimpleProofError(f"SimpleProof {context} response was not JSON") from exc
    if not isinstance(body, dict):
        raise SimpleProofError(
            f"SimpleProof {context} response was {type(body).__name__}, expected object"
        )
    return body


def _missing_response_key(
    context: str, key: str, body: dict[str, Any]
) -> SimpleProofError:
    keys = ", ".join(sorted(str(k) for k in body)) or "<none>"
    return SimpleProofError(
        f"SimpleProof {context} response missing {key!r} field (keys: {keys})"
    )


def _data_or_body(body: dict[str, Any]) -> dict[str, Any]:
    data = body.get("data")
    return data if isinstance(data, dict) else body


def _result_entries(resp: httpx.Response) -> list[Any]:
    try:
        body = resp.json()
    except ValueError as exc:
        raise SimpleProofError("SimpleProof results response was not JSON") from exc
    if isinstance(body, list):
        return body
    if not isinstance(body, dict):
        raise SimpleProofError(
            f"SimpleProof results response was {type(body).__name__}, "
            "expected object or list"
        )
    payload = _data_or_body(body)
    results = payload.get("results")
    if not isinstance(results, list):
        raise _missing_response_key("results", "results", payload)
    return results


def _entry_value(entry: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = entry.get(key)
        if value is not None:
            return str(value)
    raise _missing_response_key("results", " or ".join(repr(k) for k in keys), entry)


def _optional_int(payload: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return int(value)
    return None


def _optional_str(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return str(value)
    return None


def _response_error_detail(resp: httpx.Response) -> str | None:
    try:
        body = resp.json()
    except ValueError:
        text = resp.text.strip()
        return text[:200] if text else None
    if not isinstance(body, dict):
        return None
    for key in ("message", "msg", "error"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return None


def _retry_after_seconds(value: str, clock: Clock) -> float | None:
    try:
        delay = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except TypeError, ValueError:
            return None
        if retry_at.tzinfo is None:
            return None
        return max(0.0, (retry_at - clock.now()).total_seconds())
    return max(0.0, delay)


class SimpleProofHttpClient:
    """Real HTTP implementation.

    Callers inject a fully-configured ``httpx2.Client`` so tests can swap in
    a ``MockTransport`` and lifecycle stays caller-controlled. ``sleep`` is
    forwarded to ``ocfl_cli.ots._retry.retry`` and ``clock`` reads HTTP-date
    ``Retry-After`` values, so tests bypass wall-clock time.
    """

    submit_chunk_size = SUBMIT_CHUNK_SIZE

    def __init__(
        self,
        base_url: str,
        creds: SimpleProofCreds,
        *,
        client: httpx.Client,
        sleep: Callable[[float], None] = SYSTEM_SLEEPER.sleep,
        clock: Clock = SYSTEM_CLOCK,
        attempts: int = 3,
    ) -> None:
        self._base_url = base_url
        self._creds = creds
        self._client = client
        self._sleep = sleep
        self._clock = clock
        self._attempts = attempts

    def close(self) -> None:
        self._client.close()

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
                detail = _response_error_detail(exc.response)
                suffix = f": {detail}" if detail is not None else ""
                raise SimpleProofBadRequest(
                    f"SimpleProof rejected request: HTTP {status}{suffix}"
                ) from exc
            if status == 429:
                transient = SimpleProofTransient(
                    f"SimpleProof rate-limited: HTTP {status}"
                )
                if (retry_after := exc.response.headers.get("Retry-After")) is not None:
                    transient.retry_after = _retry_after_seconds(
                        retry_after, self._clock
                    )
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

    def check_access(self) -> None:
        """`GET /api/entity/proofCategories`: read-only, uses the access token.

        Raises `SimpleProofAuthError` on 401/403. The API key (verify jobs)
        has no read-only endpoint, so it is not checked here.
        """
        retry(
            lambda: self._request(
                "GET",
                self._base_url + _CATEGORIES_PATH,
                headers={"x-access-token": self._creds.access_token},
            ),
            sleep=self._sleep,
            attempts=self._attempts,
        )

    def submit_hashes(self, digests: list[str], category_id: int) -> SubmitResult:
        all_digests: list[str] = []
        all_record_ids: list[str] = []
        chunks = [
            digests[i : i + SUBMIT_CHUNK_SIZE]
            for i in range(0, len(digests), SUBMIT_CHUNK_SIZE)
        ]
        for chunk in chunks:
            try:
                returned_hashes = retry(
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
            all_record_ids.extend(returned_hashes)
        return SubmitResult(
            digests=tuple(all_digests),
            record_ids=tuple(all_record_ids),
        )

    def _submit_chunk(self, hashes: list[str], category_id: int) -> list[str]:
        resp = self._request(
            "POST",
            self._base_url + _SUBMIT_PATH,
            headers={"x-access-token": self._creds.access_token},
            json_body={"proofCategoryId": category_id, "hashes": hashes},
        )
        body = _json_object(resp, "submit")
        if body.get("ok") is False:
            msg = body.get("msg", "request failed")
            raise SimpleProofBadRequest(f"SimpleProof rejected request: {msg}")
        files = body.get("files")
        if not isinstance(files, list):
            raise _missing_response_key("submit", "files", body)
        return [str(file_hash) for file_hash in files]

    def verify_batch(self, digests: list[str], category_id: int) -> VerifyBatchAck:
        resp = retry(
            lambda: self._request(
                "POST",
                self._base_url + _VERIFY_PATH,
                headers={"x-api-key": self._creds.api_key},
                json_body={"proofCategoryId": category_id, "hashes": digests},
            ),
            sleep=self._sleep,
            attempts=self._attempts,
        )
        body = _json_object(resp, "verify-batch")
        payload = _data_or_body(body)
        for key in ("jobId", "job_id", "id"):
            if key in payload:
                return VerifyBatchAck(
                    job_id=str(payload[key]),
                    submitted_hashes=_optional_int(
                        payload, "submittedHashes", "submitted_hashes"
                    ),
                    total_documents_found=_optional_int(
                        payload, "totalDocumentsFound", "total_documents_found"
                    ),
                )
        raise _missing_response_key("verify-batch", "jobId", payload)

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
        body = _json_object(resp, "job-status")
        payload = _data_or_body(body)
        if "jobId" not in payload:
            raise _missing_response_key("job-status", "jobId", payload)
        if "status" not in payload:
            raise _missing_response_key("job-status", "status", payload)
        results_url = payload.get("resultsUrl")
        return JobStatus(
            job_id=str(payload["jobId"]),
            status=str(payload["status"]),
            results_url=str(results_url) if results_url is not None else None,
            total_hashes=_optional_int(payload, "totalHashes", "total_hashes"),
            verified_hashes=_optional_int(payload, "verifiedHashes", "verified_hashes"),
            created_at=_optional_str(payload, "createdAt", "created_at"),
            updated_at=_optional_str(payload, "updatedAt", "updated_at"),
        )

    def fetch_results_entries(self, results_url: str) -> tuple[ResultsEntry, ...]:
        resp = retry(
            lambda: self._request("GET", results_url),
            sleep=self._sleep,
            attempts=self._attempts,
        )
        entries: list[ResultsEntry] = []
        for entry in _result_entries(resp):
            if not isinstance(entry, dict):
                raise SimpleProofError(
                    "SimpleProof results response contained a non-object result"
                )
            detail = entry.get(
                "confirmation_detail", entry.get("confirmationDetail", ())
            )
            if detail is None:
                detail = ()
            if not isinstance(detail, list | tuple):
                raise SimpleProofError(
                    "SimpleProof results response contained non-list "
                    "confirmation_detail"
                )
            attestations: list[CalendarAttestation] = []
            for item in detail:
                if not isinstance(item, dict):
                    raise SimpleProofError(
                        "SimpleProof confirmation_detail contained a non-object item"
                    )
                attestations.append(
                    CalendarAttestation(
                        calendar=_entry_value(item, "calendar"),
                        block_height=int(
                            _entry_value(item, "blockHeight", "block_height")
                        ),
                        transaction=_entry_value(item, "transaction"),
                        merkle_root=_entry_value(item, "merkleRoot", "merkle_root"),
                    )
                )
            entries.append(
                ResultsEntry(
                    digest=_entry_value(entry, "digest", "hash"),
                    confirmed=bool(entry.get("confirmed", False)),
                    attestations=tuple(attestations),
                    ots_download_url=_entry_value(
                        entry, "otsDownloadUrl", "ots_download_url"
                    ),
                )
            )
        return tuple(entries)

    def download_results(
        self, results_url: str, expected_digests: set[str]
    ) -> SimpleProofDownloadResult:
        out: dict[str, bytes] = {}
        for entry in self.fetch_results_entries(results_url):
            claimed = entry.digest
            if claimed not in expected_digests:
                raise AttestationMismatchError(
                    f"unexpected digest from SP: {claimed} (not in submission set)"
                )
            ots_resp = retry(
                lambda u=entry.ots_download_url: self._request("GET", u),
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
        return SimpleProofDownloadResult(
            proofs=out,
            missing_digests=frozenset(expected_digests - set(out)),
        )


def default_sp_factory(cfg: Config) -> SimpleProofClient:
    """Construct the production `SimpleProofHttpClient` from `cfg`.

    Registered on `ctx.obj["sp_client_factory"]` by `main` so use case
    callers (and tests via `runner.invoke(obj=...)`) share one entry
    point for client construction.
    """
    sp_cfg = cfg.simpleproof
    if sp_cfg is None:
        raise RuntimeError("simpleproof config missing")
    client = httpx.Client(timeout=sp_cfg.timeout_s)
    creds = SimpleProofCreds(
        access_token=sp_cfg.access_token.get_secret_value(),
        api_key=sp_cfg.api_key.get_secret_value(),
    )
    return SimpleProofHttpClient(sp_cfg.base_url, creds, client=client)
