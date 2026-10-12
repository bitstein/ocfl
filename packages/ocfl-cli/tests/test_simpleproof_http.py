"""Layer-2 tests for ``SimpleProofHttpClient`` against ``httpx2.MockTransport``.

Locks in the public API and wire shapes for step 6 (cli-sp-client).

API
---
    @dataclass(frozen=True, slots=True)
    class SimpleProofCreds:
        access_token: str = ""   # x-access-token (v1 /hashes)
        api_key: str = ""        # x-api-key      (v2 verify/poll)

    class SimpleProofHttpClient:
        def __init__(
            self,
            base_url: str,
            creds: SimpleProofCreds,
            *,
            client: httpx2.Client,
            sleep: Callable[[float], None] = time.sleep,
            attempts: int = 3,
        ) -> None: ...

Wire shapes
-----------
    POST {base}/api/proof/timestamp/hashes
        headers: x-access-token: <token>
        body:    {"proofCategoryId": <int>, "hashes": [<hex>, ...]}
        200:     {"ok": true, "files": [<hex>, ...]}

    POST {base}/api/v2/proofs/verify-batches
        headers: x-api-key: <key>
        body:    {"proofCategoryId": <int>, "hashes": [<hex>, ...]}
        200:     {"jobId": <str>}

    GET {base}/api/v2/proofs/jobs/{job_id}
        headers: x-api-key: <key>
        200:     {"jobId": <str>,
                  "status": "COMPLETED"|"IN_PROGRESS"|"FAILED",
                  "resultsUrl": <str>|null}

    GET <resultsUrl>
        200: {"results": [{"digest": <hex>, "otsDownloadUrl": <str>}, ...]}

    GET <otsDownloadUrl>
        200: raw ``.ots`` bytes

Per-chunk atomicity
-------------------
    ``submit_hashes`` POSTs each ≤1000-hash chunk independently. When a
    chunk fails after retries, the proximate ``SimpleProofError`` instance
    carries a per-instance attribute ``partial_result: SubmitResult | None``
    aggregating digests/returned hashes from chunks that succeeded before the
    failure (``None`` when no chunks succeeded yet). Same per-instance
    pattern as ``Transient.retry_after``.

Parser-error mapping
--------------------
    Bad ``.ots`` bytes (``ocfl_lib._errors.OtsParseError``) downloaded from
    SP are wrapped in ``AttestationMismatchError`` (HALT) with the
    ``OtsParseError`` as ``__cause__``. SP is never trusted to hand back
    the right bytes.

Auth-header scrubbing
---------------------
    Before any ``httpx2.HTTPStatusError`` propagates, the client replaces
    ``x-access-token`` and ``x-api-key`` values on ``exc.request.headers``
    with ``<redacted>``. Plaintext credentials must not be reachable
    through the raised exception's chain.
"""

import hashlib
import json
from collections.abc import Callable

import httpx2 as httpx
import pytest

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
from ocfl_cli.ots.simpleproof import (
    SUBMIT_CHUNK_SIZE,
    VERIFY_BATCH_CHUNK_SIZE,
    SimpleProofCreds,
    SimpleProofHttpClient,
)
from ocfl_lib import OtsParseError
from tests.helpers.ots import build_attestation

# ---------------------------------------------------------------------------
# constants / helpers
# ---------------------------------------------------------------------------

BASE = "https://sp.test"
ACCESS_TOKEN = "tok-XYZ-VERY-SECRET-1234"
API_KEY = "key-ABCDEF-VERY-SECRET-9999"
CATEGORY = 641
SUBMIT_PATH = "/api/proof/timestamp/hashes"
VERIFY_PATH = "/api/v2/proofs/verify-batches"


def test_simpleproof_chunk_sizes_are_shared_from_client_module() -> None:
    from ocfl_cli.workflow import content as wf_content

    assert SUBMIT_CHUNK_SIZE == 1000
    assert VERIFY_BATCH_CHUNK_SIZE == 1000
    assert wf_content.SUBMIT_CHUNK_SIZE is SUBMIT_CHUNK_SIZE


def _digest(i: int) -> str:
    return hashlib.sha256(f"d-{i}".encode()).hexdigest()


def _digests(n: int) -> list[str]:
    return [_digest(i) for i in range(n)]


def _bytes_digest(seed: str) -> bytes:
    return hashlib.sha256(seed.encode()).digest()


def _make_sleep() -> tuple[list[float], Callable[[float], None]]:
    calls: list[float] = []
    return calls, calls.append


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    sleep: Callable[[float], None] | None = None,
    attempts: int = 3,
    creds: SimpleProofCreds | None = None,
) -> SimpleProofHttpClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    if creds is None:
        creds = SimpleProofCreds(access_token=ACCESS_TOKEN, api_key=API_KEY)
    return SimpleProofHttpClient(
        BASE,
        creds,
        client=http,
        sleep=sleep if sleep is not None else (lambda _s: None),
        attempts=attempts,
    )


def test_simpleproof_http_client_closes_injected_http_client() -> None:
    http = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200))
    )
    client = SimpleProofHttpClient(
        BASE,
        SimpleProofCreds(access_token=ACCESS_TOKEN, api_key=API_KEY),
        client=http,
    )

    client.close()

    assert http.is_closed


def _ok_submit(hashes: list[str]) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "files": hashes})


def _exc_chain_text(exc: BaseException) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        parts.append(repr(cur))
        parts.append(str(cur))
        cur = cur.__cause__ or cur.__context__
    return "\n".join(parts)


def _exc_chain_request_headers(exc: BaseException) -> list[httpx.Headers]:
    headers: list[httpx.Headers] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        req = getattr(cur, "request", None)
        if req is not None:
            headers.append(req.headers)
        cur = cur.__cause__ or cur.__context__
    return headers


# ---------------------------------------------------------------------------
# submit_hashes — chunking
# ---------------------------------------------------------------------------


class TestSubmitHashesChunking:
    def test_999_digests_one_chunk(self) -> None:
        digests = _digests(999)
        captured: list[list[str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == BASE + SUBMIT_PATH
            payload = json.loads(request.content)
            captured.append(list(payload["hashes"]))
            assert payload["proofCategoryId"] == CATEGORY
            return _ok_submit(payload["hashes"])

        client = _make_client(handler)

        result = client.submit_hashes(digests, CATEGORY)

        assert len(captured) == 1
        assert len(captured[0]) == 999
        assert isinstance(result, SubmitResult)
        assert result.digests == tuple(digests)
        assert len(result.record_ids) == 999

    def test_1000_digests_one_chunk(self) -> None:
        digests = _digests(1000)
        captured: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            captured.append(len(payload["hashes"]))
            return _ok_submit(payload["hashes"])

        client = _make_client(handler)

        result = client.submit_hashes(digests, CATEGORY)

        assert captured == [1000]
        assert result.digests == tuple(digests)
        assert len(result.record_ids) == 1000

    def test_1001_digests_two_chunks_1000_then_1(self) -> None:
        digests = _digests(1001)
        sizes: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            sizes.append(len(payload["hashes"]))
            return _ok_submit(payload["hashes"])

        client = _make_client(handler)

        result = client.submit_hashes(digests, CATEGORY)

        assert sizes == [1000, 1]
        assert result.digests == tuple(digests)
        assert len(result.record_ids) == 1001
        assert result.record_ids == tuple(digests)

    def test_sets_x_access_token_header(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("x-access-token", ""))
            payload = json.loads(request.content)
            return _ok_submit(payload["hashes"])

        client = _make_client(handler)

        client.submit_hashes(_digests(3), CATEGORY)

        assert seen == [ACCESS_TOKEN]


# ---------------------------------------------------------------------------
# submit_hashes — error mapping + retries
# ---------------------------------------------------------------------------


class TestSubmitHashesErrors:
    def test_401_raises_auth_error_no_retry(self) -> None:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(401, content=b"unauthorized")

        sleeps, sleep = _make_sleep()
        client = _make_client(handler, sleep=sleep)

        with pytest.raises(SimpleProofAuthError):
            client.submit_hashes(_digests(3), CATEGORY)

        assert len(attempts) == 1
        assert sleeps == []

    def test_403_raises_auth_error_no_retry(self) -> None:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(403, content=b"forbidden")

        sleeps, sleep = _make_sleep()
        client = _make_client(handler, sleep=sleep)

        with pytest.raises(SimpleProofAuthError):
            client.submit_hashes(_digests(3), CATEGORY)

        assert len(attempts) == 1
        assert sleeps == []

    @pytest.mark.parametrize("status", [400, 422])
    def test_4xx_bad_request_no_retry(self, status: int) -> None:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(status, content=b"bad")

        sleeps, sleep = _make_sleep()
        client = _make_client(handler, sleep=sleep)

        with pytest.raises(SimpleProofBadRequest):
            client.submit_hashes(_digests(3), CATEGORY)

        assert len(attempts) == 1
        assert sleeps == []

    def test_200_missing_files_raises_simpleproof_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"message": "ok"})

        client = _make_client(handler)

        with pytest.raises(SimpleProofError) as info:
            client.submit_hashes(_digests(3), CATEGORY)

        assert "submit response missing 'files'" in str(info.value)
        assert "message" in str(info.value)

    def test_200_ok_false_raises_bad_request_with_message(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": False, "msg": "bad hash"})

        client = _make_client(handler)

        with pytest.raises(SimpleProofBadRequest) as info:
            client.submit_hashes(_digests(1), CATEGORY)

        assert "bad hash" in str(info.value)

    def test_500_twice_then_200(self) -> None:
        n = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal n
            n += 1
            if n < 3:
                return httpx.Response(500, content=b"boom")
            payload = json.loads(request.content)
            return _ok_submit(payload["hashes"])

        sleeps, sleep = _make_sleep()
        client = _make_client(handler, sleep=sleep, attempts=3)

        result = client.submit_hashes(_digests(2), CATEGORY)

        assert n == 3
        assert len(sleeps) == 2
        assert isinstance(result, SubmitResult)
        assert len(result.record_ids) == 2

    def test_backoff_for_transient_submit_retry(self) -> None:
        n = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal n
            n += 1
            if n < 4:
                return httpx.Response(429, content=b"slow")
            payload = json.loads(request.content)
            return _ok_submit(payload["hashes"])

        sleeps, sleep = _make_sleep()
        client = _make_client(handler, sleep=sleep, attempts=4)

        result = client.submit_hashes(_digests(2), CATEGORY)

        assert result.record_ids == tuple(_digests(2))
        assert n == 4
        assert sleeps == [2.0, 4.0, 8.0]

    def test_429_retry_after_populates_attribute_and_sleep(self) -> None:
        n = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal n
            n += 1
            return httpx.Response(429, headers={"Retry-After": "5"}, content=b"slow")

        sleeps, sleep = _make_sleep()
        client = _make_client(handler, sleep=sleep, attempts=3)

        with pytest.raises(SimpleProofTransient) as info:
            client.submit_hashes(_digests(2), CATEGORY)

        assert info.value.retry_after == 5.0
        assert n == 3
        # 2 retries -> 2 sleeps; both honor Retry-After.
        assert sleeps == [5.0, 5.0]

    def test_429_malformed_retry_after_uses_backoff(self) -> None:
        n = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal n
            n += 1
            if n == 1:
                return httpx.Response(
                    429,
                    headers={"Retry-After": "not-a-delay"},
                    content=b"slow",
                )
            payload = json.loads(request.content)
            return _ok_submit(payload["hashes"])

        sleeps, sleep = _make_sleep()
        client = _make_client(handler, sleep=sleep)

        client.submit_hashes(_digests(2), CATEGORY)

        assert n == 2
        assert sleeps == [2.0]

    def test_timeout_raises_transient(self) -> None:
        n = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal n
            n += 1
            raise httpx.ReadTimeout("read timeout", request=request)

        sleeps, sleep = _make_sleep()
        client = _make_client(handler, sleep=sleep, attempts=3)

        with pytest.raises(SimpleProofTransient):
            client.submit_hashes(_digests(2), CATEGORY)

        assert n == 3
        assert len(sleeps) == 2

    def test_connect_error_raises_transient(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("conn refused")

        sleeps, sleep = _make_sleep()
        client = _make_client(handler, sleep=sleep, attempts=3)

        with pytest.raises(SimpleProofTransient):
            client.submit_hashes(_digests(2), CATEGORY)

        assert len(sleeps) == 2


# ---------------------------------------------------------------------------
# verify_batch
# ---------------------------------------------------------------------------


class TestVerifyBatch:
    def test_posts_to_verify_batches_with_api_key(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"jobId": "job-abc-123"})

        client = _make_client(handler)
        digests = _digests(5)

        ack = client.verify_batch(digests, CATEGORY)

        assert ack == VerifyBatchAck(
            job_id="job-abc-123",
            submitted_hashes=None,
            total_documents_found=None,
        )
        assert len(captured) == 1
        req = captured[0]
        assert req.method == "POST"
        assert str(req.url) == BASE + VERIFY_PATH
        assert req.headers.get("x-api-key") == API_KEY
        # /hashes auth header MUST NOT be sent on v2
        assert "x-access-token" not in req.headers
        body = json.loads(req.content)
        assert body == {"proofCategoryId": CATEGORY, "hashes": digests}

    def test_decodes_verify_batch_data_envelope(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"status": "success", "data": {"jobId": "job-nested-123"}},
            )

        client = _make_client(handler)

        assert client.verify_batch(_digests(1), CATEGORY).job_id == "job-nested-123"

    def test_decodes_verify_batch_ack_counts(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                202,
                json={
                    "data": {
                        "jobId": "job-counts",
                        "submittedHashes": 10,
                        "totalDocumentsFound": 7,
                    }
                },
            )

        client = _make_client(handler)

        assert client.verify_batch(_digests(10), CATEGORY) == VerifyBatchAck(
            job_id="job-counts",
            submitted_hashes=10,
            total_documents_found=7,
        )

    def test_400_includes_response_message(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={
                    "status": "error",
                    "message": "Missing proofCategoryId or empty hashes array",
                },
            )

        client = _make_client(handler)

        with pytest.raises(SimpleProofBadRequest) as info:
            client.verify_batch(_digests(1), CATEGORY)

        assert str(info.value) == (
            "SimpleProof rejected request: HTTP 400: "
            "Missing proofCategoryId or empty hashes array"
        )


# ---------------------------------------------------------------------------
# poll_job
# ---------------------------------------------------------------------------


class TestPollJob:
    def test_decodes_completed_job(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={
                    "jobId": "job-xyz",
                    "status": "COMPLETED",
                    "resultsUrl": "https://sp-results.test/job-xyz",
                    "totalHashes": 250,
                    "verifiedHashes": 187,
                    "createdAt": "2026-05-15T12:00:00Z",
                    "updatedAt": "2026-05-15T12:03:00Z",
                },
            )

        client = _make_client(handler)

        status = client.poll_job("job-xyz")

        assert isinstance(status, JobStatus)
        assert status.job_id == "job-xyz"
        assert status.status == "COMPLETED"
        assert status.results_url == "https://sp-results.test/job-xyz"
        assert status.total_hashes == 250
        assert status.verified_hashes == 187
        assert status.created_at == "2026-05-15T12:00:00Z"
        assert status.updated_at == "2026-05-15T12:03:00Z"
        assert len(captured) == 1
        req = captured[0]
        assert req.method == "GET"
        assert str(req.url) == BASE + "/api/v2/proofs/jobs/job-xyz"
        assert req.headers.get("x-api-key") == API_KEY

    def test_decodes_in_progress_job_no_results_url(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "jobId": "job-pending",
                    "status": "IN_PROGRESS",
                    "resultsUrl": None,
                },
            )

        client = _make_client(handler)

        status = client.poll_job("job-pending")

        assert status.status == "IN_PROGRESS"
        assert status.results_url is None

    def test_decodes_job_status_data_envelope(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "jobId": "job-nested",
                        "status": "COMPLETED",
                        "resultsUrl": "https://sp-results.test/job-nested",
                    },
                },
            )

        client = _make_client(handler)

        status = client.poll_job("job-nested")

        assert status.job_id == "job-nested"
        assert status.status == "COMPLETED"
        assert status.results_url == "https://sp-results.test/job-nested"

    def test_decodes_failed_job(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"jobId": "job-fail", "status": "FAILED", "resultsUrl": None},
            )

        client = _make_client(handler)

        status = client.poll_job("job-fail")

        assert status.status == "FAILED"

    def test_decodes_expired_job(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"jobId": "job-expired", "status": "EXPIRED", "resultsUrl": None},
            )

        client = _make_client(handler)

        status = client.poll_job("job-expired")

        assert status.status == "EXPIRED"


# ---------------------------------------------------------------------------
# download_results
# ---------------------------------------------------------------------------


class TestDownloadResults:
    def test_fetch_results_entries_parses_confirmation_detail(self) -> None:
        digest = _bytes_digest("file-a").hex()
        results_url = "https://sp-results.test/job-progress"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "digest": digest,
                            "confirmed": True,
                            "otsDownloadUrl": "https://sp-blobs.test/a.ots",
                            "confirmation_detail": [
                                {
                                    "calendar": "bob.btc.calendar.opentimestamps.org",
                                    "blockHeight": 920150,
                                    "transaction": "txid",
                                    "merkleRoot": "root",
                                }
                            ],
                        }
                    ]
                },
            )

        client = _make_client(handler)

        assert client.fetch_results_entries(results_url) == (
            ResultsEntry(
                digest=digest,
                confirmed=True,
                attestations=(
                    CalendarAttestation(
                        calendar="bob.btc.calendar.opentimestamps.org",
                        block_height=920150,
                        transaction="txid",
                        merkle_root="root",
                    ),
                ),
                ots_download_url="https://sp-blobs.test/a.ots",
            ),
        )

    def test_happy_path_returns_dict_keyed_by_digest(self) -> None:
        digest_a_bytes = _bytes_digest("file-a")
        digest_b_bytes = _bytes_digest("file-b")
        digest_a = digest_a_bytes.hex()
        digest_b = digest_b_bytes.hex()
        ots_a = build_attestation(digest_a_bytes, block_height=800_000)
        ots_b = build_attestation(digest_b_bytes, block_height=800_001)

        results_url = "https://sp-results.test/job-1"
        ots_url_a = "https://sp-blobs.test/a.ots"
        ots_url_b = "https://sp-blobs.test/b.ots"

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == results_url:
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {"digest": digest_a, "otsDownloadUrl": ots_url_a},
                            {"digest": digest_b, "otsDownloadUrl": ots_url_b},
                        ],
                    },
                )
            if url == ots_url_a:
                return httpx.Response(200, content=ots_a)
            if url == ots_url_b:
                return httpx.Response(200, content=ots_b)
            return httpx.Response(404, content=b"unknown url: " + url.encode())

        client = _make_client(handler)

        out = client.download_results(results_url, {digest_a, digest_b})

        assert isinstance(out, SimpleProofDownloadResult)
        assert set(out) == {digest_a, digest_b}
        assert out[digest_a] == ots_a
        assert out[digest_b] == ots_b
        assert out.proofs == {digest_a: ots_a, digest_b: ots_b}
        assert out.missing_digests == frozenset()

    def test_missing_expected_digest_is_explicit(self) -> None:
        digest_a_bytes = _bytes_digest("file-a")
        digest_b_bytes = _bytes_digest("file-b")
        digest_a = digest_a_bytes.hex()
        digest_b = digest_b_bytes.hex()
        ots_a = build_attestation(digest_a_bytes, block_height=800_000)

        results_url = "https://sp-results.test/job-partial"
        ots_url_a = "https://sp-blobs.test/a.ots"

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == results_url:
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {"digest": digest_a, "otsDownloadUrl": ots_url_a},
                        ],
                    },
                )
            if url == ots_url_a:
                return httpx.Response(200, content=ots_a)
            return httpx.Response(404)

        client = _make_client(handler)

        out = client.download_results(results_url, {digest_a, digest_b})

        assert out.proofs == {digest_a: ots_a}
        assert out.missing_digests == frozenset({digest_b})

    def test_decodes_results_data_envelope(self) -> None:
        digest_bytes = _bytes_digest("file-envelope")
        digest = digest_bytes.hex()
        ots_bytes = build_attestation(digest_bytes, block_height=800_000)

        results_url = "https://sp-results.test/job-envelope"
        ots_url = "https://sp-blobs.test/envelope.ots"

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == results_url:
                return httpx.Response(
                    200,
                    json={
                        "status": "success",
                        "data": {
                            "results": [
                                {"digest": digest, "otsDownloadUrl": ots_url},
                            ],
                        },
                    },
                )
            if url == ots_url:
                return httpx.Response(200, content=ots_bytes)
            return httpx.Response(404)

        client = _make_client(handler)

        assert client.download_results(results_url, {digest}).proofs == {
            digest: ots_bytes
        }

    def test_decodes_results_list_with_simpleproof_field_names(self) -> None:
        digest_bytes = _bytes_digest("file-list")
        digest = digest_bytes.hex()
        ots_bytes = build_attestation(digest_bytes, block_height=800_000)

        results_url = "https://sp-results.test/job-list"
        ots_url = "https://sp-blobs.test/list.ots"

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == results_url:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "confirmed": True,
                            "hash": digest,
                            "ots_download_url": ots_url,
                        }
                    ],
                )
            if url == ots_url:
                return httpx.Response(200, content=ots_bytes)
            return httpx.Response(404)

        client = _make_client(handler)

        assert client.download_results(results_url, {digest}).proofs == {
            digest: ots_bytes
        }

    def test_commitment_digest_mismatch_raises_attestation_mismatch(self) -> None:
        # .ots bytes commit to digest X but the server claims they belong
        # to digest Y. SP is never trusted: HALT.
        actual_bytes = _bytes_digest("real-file")
        claimed_bytes = _bytes_digest("WRONG-file")
        ots_bytes = build_attestation(actual_bytes, block_height=800_000)
        claimed_hex = claimed_bytes.hex()

        results_url = "https://sp-results.test/mismatch"
        ots_url = "https://sp-blobs.test/x.ots"

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == results_url:
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {"digest": claimed_hex, "otsDownloadUrl": ots_url},
                        ],
                    },
                )
            if url == ots_url:
                return httpx.Response(200, content=ots_bytes)
            return httpx.Response(404)

        client = _make_client(handler)

        with pytest.raises(AttestationMismatchError):
            client.download_results(results_url, {claimed_hex})

    def test_truncated_ots_bytes_raises_attestation_mismatch(self) -> None:
        # Truncated/malformed .ots → OtsParseError → wrapped in
        # AttestationMismatchError (HALT) with OtsParseError as __cause__.
        good = _bytes_digest("file-c")
        good_hex = good.hex()
        truncated = build_attestation(good, block_height=800_000)[:8]

        results_url = "https://sp-results.test/bad"
        ots_url = "https://sp-blobs.test/bad.ots"

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == results_url:
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {"digest": good_hex, "otsDownloadUrl": ots_url},
                        ],
                    },
                )
            if url == ots_url:
                return httpx.Response(200, content=truncated)
            return httpx.Response(404)

        client = _make_client(handler)

        with pytest.raises(AttestationMismatchError) as info:
            client.download_results(results_url, {good_hex})

        # Underlying parser failure preserved in cause chain.
        cause: BaseException | None = info.value.__cause__
        assert isinstance(cause, OtsParseError)

    def test_unexpected_digest_from_sp_raises_attestation_mismatch(self) -> None:
        # SP returns a digest the caller never submitted. Even if the .ots
        # bytes self-commit cleanly, treat as HALT — SP is never trusted.
        submitted_bytes = _bytes_digest("submitted-file")
        rogue_bytes = _bytes_digest("rogue-file")
        submitted_hex = submitted_bytes.hex()
        rogue_hex = rogue_bytes.hex()
        rogue_ots = build_attestation(rogue_bytes, block_height=800_000)

        results_url = "https://sp-results.test/rogue"
        ots_url = "https://sp-blobs.test/rogue.ots"

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == results_url:
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {"digest": rogue_hex, "otsDownloadUrl": ots_url},
                        ],
                    },
                )
            if url == ots_url:
                return httpx.Response(200, content=rogue_ots)
            return httpx.Response(404)

        client = _make_client(handler)

        with pytest.raises(AttestationMismatchError, match="not in submission set"):
            client.download_results(results_url, {submitted_hex})


# ---------------------------------------------------------------------------
# auth-header scrubbing
# ---------------------------------------------------------------------------


class TestAuthHeaderScrubbing:
    def _assert_no_plaintext_creds(self, exc: BaseException) -> None:
        chain_text = _exc_chain_text(exc)
        assert ACCESS_TOKEN not in chain_text
        assert API_KEY not in chain_text

        for headers in _exc_chain_request_headers(exc):
            for name in ("x-access-token", "x-api-key"):
                value = headers.get(name)
                if value is None:
                    continue
                assert ACCESS_TOKEN not in value
                assert API_KEY not in value

    def test_5xx_path_scrubs_x_access_token(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["token"] = request.headers.get("x-access-token", "")
            return httpx.Response(503, content=b"upstream down")

        _sleeps, sleep = _make_sleep()
        client = _make_client(handler, sleep=sleep, attempts=2)

        with pytest.raises(SimpleProofTransient) as info:
            client.submit_hashes(_digests(3), CATEGORY)

        # Sanity: wire request actually carried the token.
        assert seen["token"] == ACCESS_TOKEN
        self._assert_no_plaintext_creds(info.value)

    def test_401_path_scrubs_x_access_token(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, content=b"no")

        client = _make_client(handler)

        with pytest.raises(SimpleProofAuthError) as info:
            client.submit_hashes(_digests(3), CATEGORY)

        self._assert_no_plaintext_creds(info.value)

    def test_5xx_path_scrubs_x_api_key_on_verify_batch(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["key"] = request.headers.get("x-api-key", "")
            return httpx.Response(502, content=b"bad gateway")

        _sleeps, sleep = _make_sleep()
        client = _make_client(handler, sleep=sleep, attempts=2)

        with pytest.raises(SimpleProofTransient) as info:
            client.verify_batch(_digests(3), CATEGORY)

        assert seen["key"] == API_KEY
        self._assert_no_plaintext_creds(info.value)


# ---------------------------------------------------------------------------
# per-chunk atomicity
# ---------------------------------------------------------------------------


class TestSubmitHashesAtomicity:
    """When a chunk fails after retries, prior chunks' SubmitResult is
    surfaced via ``exc.partial_result``. Same per-instance attribute
    pattern as ``Transient.retry_after``.
    """

    def test_first_chunk_ok_second_chunk_400_partial_result_attached(self) -> None:
        digests = _digests(1500)
        n = 0
        chunk1_payload: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal n
            n += 1
            payload = json.loads(request.content)
            if n == 1:
                # First chunk: 1000 hashes succeed.
                chunk1_payload.extend(payload["hashes"])
                return _ok_submit(payload["hashes"])
            # Second chunk (1=2): 500 hashes — server rejects.
            return httpx.Response(400, content=b"bad")

        sleeps, sleep = _make_sleep()
        client = _make_client(handler, sleep=sleep, attempts=3)

        with pytest.raises(SimpleProofBadRequest) as info:
            client.submit_hashes(digests, CATEGORY)

        # First chunk wired correctly.
        assert chunk1_payload == digests[:1000]
        # Bad-request fails fast → no retry on the failing chunk.
        assert n == 2
        assert sleeps == []

        partial = info.value.partial_result
        assert isinstance(partial, SubmitResult)
        assert partial.digests == tuple(digests[:1000])
        assert len(partial.record_ids) == 1000

    def test_first_chunk_fails_partial_result_is_none(self) -> None:
        digests = _digests(1500)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, content=b"bad")

        client = _make_client(handler)

        with pytest.raises(SimpleProofBadRequest) as info:
            client.submit_hashes(digests, CATEGORY)

        assert info.value.partial_result is None
