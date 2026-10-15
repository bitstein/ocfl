import json
from collections.abc import Callable

import httpx2 as httpx
import pytest
from exc_chain import exc_chain_request_headers, exc_chain_text

from ocfl_lib import BitcoinNoBlockError, BitcoinRpcAuthError, BitcoinRpcTransientError
from ocfl_lib.bitcoin import BitcoinRpcClient, BlockchainInfo, BlockHeader


def _rpc_ok(result: object) -> bytes:
    return json.dumps({"result": result, "error": None, "id": 1}).encode()


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    auth: httpx.Auth | None = None,
    sleep: Callable[[float], None] | None = None,
    attempts: int = 3,
) -> BitcoinRpcClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, auth=auth)
    return BitcoinRpcClient(
        "http://node:8332/",
        client=http,
        sleep=sleep if sleep is not None else (lambda _s: None),
        attempts=attempts,
    )


def _make_sleep() -> tuple[list[float], Callable[[float], None]]:
    calls: list[float] = []
    return calls, calls.append


def test_bitcoin_rpc_client_closes_injected_http_client() -> None:
    http = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200))
    )
    client = BitcoinRpcClient("http://node:8332/", client=http)

    client.close()

    assert http.is_closed


class TestGetBlockchainInfo:
    def test_decodes_blockchain_info(self) -> None:
        captured_method: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            captured_method.append(payload["method"])
            return httpx.Response(
                200,
                content=_rpc_ok(
                    {
                        "blocks": 800_000,
                        "bestblockhash": "0000abcd",
                        "initialblockdownload": False,
                        "verificationprogress": 1.0,
                    }
                ),
            )

        rpc = _make_client(handler)

        info = rpc.getblockchaininfo()

        assert captured_method == ["getblockchaininfo"]
        assert isinstance(info, BlockchainInfo)
        assert info.height == 800_000
        assert info.best_block_hash == "0000abcd"
        assert info.initial_block_download is False
        assert info.verification_progress == 1.0


class TestGetBlockHeader:
    def test_decodes_block_header(self) -> None:
        captured_method: list[str] = []
        captured_params: list[list[object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            captured_method.append(payload["method"])
            captured_params.append(payload["params"])
            return httpx.Response(
                200,
                content=_rpc_ok(
                    {
                        "hash": "0000abcd",
                        "confirmations": 6,
                        "height": 800_000,
                        "merkleroot": "deadbeef" * 8,
                        "time": 1700000000,
                        "nonce": 0,
                    }
                ),
            )

        rpc = _make_client(handler)

        header = rpc.getblockheader("0000abcd")

        assert captured_method == ["getblockheader"]
        assert captured_params == [["0000abcd", True]]
        assert isinstance(header, BlockHeader)
        assert header.block_hash == "0000abcd"
        assert header.height == 800_000
        assert header.confirmations == 6
        assert header.merkle_root == "deadbeef" * 8
        assert header.time == 1700000000


class TestGetBlockHash:
    def test_returns_block_hash_for_height(self) -> None:
        captured_method: list[str] = []
        captured_params: list[list[object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            captured_method.append(payload["method"])
            captured_params.append(payload["params"])
            return httpx.Response(200, content=_rpc_ok("0000feedface"))

        rpc = _make_client(handler)

        block_hash = rpc.getblockhash(800_000)

        assert captured_method == ["getblockhash"]
        assert captured_params == [[800_000]]
        assert block_hash == "0000feedface"


class TestErrorMapping:
    def test_401_raises_bitcoin_auth_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, content=b"unauthorized")

        rpc = _make_client(handler)

        with pytest.raises(BitcoinRpcAuthError):
            rpc.getblockchaininfo()

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_5xx_raises_bitcoin_rpc_transient(self, status: int) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, content=b"server boom")

        rpc = _make_client(handler)

        with pytest.raises(BitcoinRpcTransientError):
            rpc.getblockchaininfo()

    def test_429_retries_with_backoff_then_succeeds(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls < 3:
                return httpx.Response(429, content=b"too many requests")
            return httpx.Response(
                200,
                content=_rpc_ok(
                    {
                        "blocks": 800_000,
                        "bestblockhash": "0000abcd",
                        "initialblockdownload": False,
                        "verificationprogress": 1.0,
                    }
                ),
            )

        sleeps, sleep = _make_sleep()
        rpc = _make_client(handler, sleep=sleep)

        info = rpc.getblockchaininfo()

        assert info.height == 800_000
        assert calls == 3
        assert sleeps == [2.0, 4.0]

    def test_429_exhausts_retry_budget(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(429, content=b"too many requests")

        sleeps, sleep = _make_sleep()
        rpc = _make_client(handler, sleep=sleep)

        with pytest.raises(BitcoinRpcTransientError):
            rpc.getblockchaininfo()

        assert calls == 3
        assert sleeps == [2.0, 4.0]

    def test_timeout_raises_bitcoin_rpc_transient(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("read timeout", request=request)

        rpc = _make_client(handler)

        with pytest.raises(BitcoinRpcTransientError):
            rpc.getblockchaininfo()

    def test_connect_error_raises_bitcoin_rpc_transient(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("conn refused")

        rpc = _make_client(handler)

        with pytest.raises(BitcoinRpcTransientError):
            rpc.getblockchaininfo()

    def test_rpc_loading_error_retries_with_backoff(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls < 3:
                return httpx.Response(
                    200,
                    json={
                        "result": None,
                        "error": {"code": -28, "message": "Loading block index"},
                        "id": 1,
                    },
                )
            return httpx.Response(
                200,
                content=_rpc_ok(
                    {
                        "blocks": 800_000,
                        "bestblockhash": "0000abcd",
                        "initialblockdownload": False,
                        "verificationprogress": 1.0,
                    }
                ),
            )

        sleeps, sleep = _make_sleep()
        rpc = _make_client(
            handler,
            sleep=sleep,
        )

        info = rpc.getblockchaininfo()

        assert info.height == 800_000
        assert calls == 3
        assert sleeps == [2.0, 4.0]

    def test_getblockhash_200_out_of_range_raises_block_missing(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                200,
                json={
                    "result": None,
                    "error": {"code": -8, "message": "Block height out of range"},
                    "id": 1,
                },
            )

        sleeps, sleep = _make_sleep()
        rpc = _make_client(handler, sleep=sleep)

        with pytest.raises(BitcoinNoBlockError):
            rpc.getblockhash(-1)

        assert calls == 1
        assert sleeps == []

    def test_getblockhash_500_out_of_range_raises_block_missing(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                500,
                json={
                    "result": None,
                    "error": {"code": -8, "message": "Block height out of range"},
                    "id": 1,
                },
            )

        sleeps, sleep = _make_sleep()
        rpc = _make_client(handler, sleep=sleep)

        with pytest.raises(BitcoinNoBlockError):
            rpc.getblockhash(800_000)

        assert calls == 1
        assert sleeps == []

    def test_500_non_object_json_error_body_raises_transient(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(500, json=["not", "a", "json-rpc", "object"])

        sleeps, sleep = _make_sleep()
        rpc = _make_client(handler, sleep=sleep)

        with pytest.raises(BitcoinRpcTransientError):
            rpc.getblockchaininfo()

        assert calls == 3
        assert sleeps == [2.0, 4.0]


class TestAuthHeaderScrubbing:
    _USER = "rpcuser"
    _PASSWORD = "super-secret-password"

    def _assert_no_plaintext(self, exc: BaseException) -> None:
        chain_text = exc_chain_text(exc)
        assert self._PASSWORD not in chain_text
        import base64

        token = base64.b64encode(f"{self._USER}:{self._PASSWORD}".encode()).decode()
        assert token not in chain_text

        for headers in exc_chain_request_headers(exc):
            auth_value = headers.get("Authorization")
            if auth_value is None:
                continue
            assert self._PASSWORD not in auth_value
            assert token not in auth_value

    def test_5xx_propagates_with_scrubbed_authorization(self) -> None:
        seen_auth: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_auth["value"] = request.headers.get("Authorization", "")
            return httpx.Response(503, content=b"upstream down")

        auth = httpx.BasicAuth(self._USER, self._PASSWORD)
        rpc = _make_client(handler, auth=auth)

        with pytest.raises(BitcoinRpcTransientError) as info:
            rpc.getblockchaininfo()

        assert seen_auth["value"].startswith("Basic "), "request carried Basic auth"
        self._assert_no_plaintext(info.value)

    def test_401_propagates_with_scrubbed_authorization(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, content=b"no")

        auth = httpx.BasicAuth(self._USER, self._PASSWORD)
        rpc = _make_client(handler, auth=auth)

        with pytest.raises(BitcoinRpcAuthError) as info:
            rpc.getblockchaininfo()

        self._assert_no_plaintext(info.value)
