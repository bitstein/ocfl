"""Layer-2 tests for ``BitcoinRPCClient`` against ``httpx.MockTransport``.

The client must accept an injected ``httpx.Client`` so tests can swap in a
``MockTransport``. Production callers build their own ``Client`` with
``httpx.BasicAuth`` (rpc_user/password) and a timeout.
"""

import json
from collections.abc import Callable

import httpx
import pytest

from ocfl_cli.errors import BitcoinAuthError, BitcoinRPCTransient
from ocfl_cli.ots.bitcoin import BitcoinRPCClient, BlockchainInfo, BlockHeader


def _rpc_ok(result: object) -> bytes:
    return json.dumps({"result": result, "error": None, "id": 1}).encode()


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    auth: httpx.Auth | None = None,
) -> BitcoinRPCClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, auth=auth)
    return BitcoinRPCClient("http://node:8332/", client=http)


def _exc_chain_text(exc: BaseException) -> str:
    """Concatenate str(exc) across the full __cause__/__context__ chain."""
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
    """Collect every ``request.headers`` reachable through the chain."""
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
        # Bitcoin Core's getblockheader takes (block_hash, verbose=true)
        assert captured_params[0][0] == "0000abcd"
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

        with pytest.raises(BitcoinAuthError):
            rpc.getblockchaininfo()

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_5xx_raises_bitcoin_rpc_transient(self, status: int) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, content=b"server boom")

        rpc = _make_client(handler)

        with pytest.raises(BitcoinRPCTransient):
            rpc.getblockchaininfo()

    def test_timeout_raises_bitcoin_rpc_transient(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("read timeout", request=request)

        rpc = _make_client(handler)

        with pytest.raises(BitcoinRPCTransient):
            rpc.getblockchaininfo()

    def test_connect_error_raises_bitcoin_rpc_transient(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("conn refused")

        rpc = _make_client(handler)

        with pytest.raises(BitcoinRPCTransient):
            rpc.getblockchaininfo()


class TestAuthHeaderScrubbing:
    """Authorization plaintext must never escape via the propagated exception."""

    _USER = "rpcuser"
    _PASSWORD = "super-secret-password"

    def _assert_no_plaintext(self, exc: BaseException) -> None:
        chain_text = _exc_chain_text(exc)
        assert self._PASSWORD not in chain_text
        # Base64("rpcuser:super-secret-password")
        import base64

        token = base64.b64encode(f"{self._USER}:{self._PASSWORD}".encode()).decode()
        assert token not in chain_text

        for headers in _exc_chain_request_headers(exc):
            auth_value = headers.get("Authorization")
            if auth_value is None:
                continue
            assert self._PASSWORD not in auth_value
            assert token not in auth_value
            # Some explicit redaction marker is preferred but we only require
            # absence of plaintext above.

    def test_5xx_propagates_with_scrubbed_authorization(self) -> None:
        seen_auth: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_auth["value"] = request.headers.get("Authorization", "")
            return httpx.Response(503, content=b"upstream down")

        auth = httpx.BasicAuth(self._USER, self._PASSWORD)
        rpc = _make_client(handler, auth=auth)

        with pytest.raises(BitcoinRPCTransient) as info:
            rpc.getblockchaininfo()

        # Sanity: the wire request actually carried Basic auth.
        assert seen_auth["value"].startswith("Basic ")
        self._assert_no_plaintext(info.value)

    def test_401_propagates_with_scrubbed_authorization(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, content=b"no")

        auth = httpx.BasicAuth(self._USER, self._PASSWORD)
        rpc = _make_client(handler, auth=auth)

        with pytest.raises(BitcoinAuthError) as info:
            rpc.getblockchaininfo()

        self._assert_no_plaintext(info.value)
