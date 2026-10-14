import hashlib
import json

import httpx2 as httpx
import pytest

from ocfl_lib import (
    BitcoinNoBlockError,
    BitcoinRpcAuthError,
    BitcoinRpcError,
    BitcoinRpcTransientError,
    ots,
)
from ocfl_lib.bitcoin import BitcoinRpcClient
from ocfl_lib.ots import AttestationStatus, ChainBlock, ChainCheck
from tests.helpers.ots import build_attestation, build_committed_attestation


class Chain:
    def __init__(self, tip: int, roots: dict[int, bytes]) -> None:
        self.tip = tip
        self.roots = roots

    def block_at(self, height: int) -> ChainBlock | None:
        if height > self.tip:
            return None
        root = self.roots.get(height, b"\0" * 32)
        return ChainBlock(height, f"{height:064x}", root, self.tip - height + 1)


def _check(chain: Chain, raw: bytes, n: int = 3):
    return ots.check_attestations(ots.parse(raw), chain, confirmations=n)


def test_statuses():
    raw, root = build_committed_attestation(b"\1" * 32, block_height=10)
    assert [c.status for c in _check(Chain(20, {10: root}), raw)] == [
        AttestationStatus.VERIFIED
    ]
    assert [c.status for c in _check(Chain(20, {}), raw)] == [
        AttestationStatus.MISMATCH
    ]
    assert [c.status for c in _check(Chain(11, {}), raw)] == [AttestationStatus.SHALLOW]
    assert [c.block for c in _check(Chain(9, {}), raw)] == [None]


def test_pending_has_no_checks():
    raw = build_attestation(b"\1" * 32, pending_uri="https://x")
    assert _check(Chain(100, {}), raw) == ()


def test_from_rpc_reverses_display_hex():
    b = ChainBlock.from_rpc(
        height=1, block_hash="h", merkleroot="0102" + "00" * 30, confirmations=1
    )
    assert b.merkle_root == b"\0" * 30 + b"\2\1"


def _object(tmp_path, height: int):
    from ocfl_lib import LocalFS

    data = b"x"
    proof = build_attestation(hashlib.sha256(data).digest(), block_height=height)
    d, p = hashlib.sha256(data).hexdigest(), hashlib.sha256(proof).hexdigest()
    inv = {
        "id": "o",
        "type": "https://ocfl.io/1.1/spec/#inventory",
        "digestAlgorithm": "sha256",
        "head": "v1",
        "manifest": {d: ["v1/content/x"], p: ["v1/content/x.ots"]},
        "versions": {
            "v1": {
                "created": "2026-01-01T00:00:00Z",
                "state": {d: ["x"], p: ["x.ots"]},
            }
        },
    }
    raw = json.dumps(inv).encode()
    o = tmp_path / "o"
    (o / "v1/content").mkdir(parents=True)
    (o / "0=ocfl_object_1.1").write_text("ocfl_object_1.1\n")
    for where in (o, o / "v1"):
        (where / "inventory.json").write_bytes(raw)
        (where / "inventory.json.sha256").write_text(
            f"{hashlib.sha256(raw).hexdigest()} inventory.json\n"
        )
    (o / "v1/content/x").write_bytes(data)
    (o / "v1/content/x.ots").write_bytes(proof)
    (o / ots.EXTENSION_DIR).mkdir(parents=True)
    (o / ots.CONFIG_PATH).write_text(json.dumps({"extensionName": ots.EXTENSION_NAME}))
    inv_proof = build_attestation(hashlib.sha256(raw).digest(), block_height=height + 1)
    (o / ots.inventory_proof_path("v1")).write_bytes(inv_proof)
    roots = {
        height: hashlib.sha256(data).digest(),
        height + 1: hashlib.sha256(raw).digest(),
    }
    return LocalFS(tmp_path), roots


def test_validate_object_chain(tmp_path):
    from ocfl_lib.validation import validate_object

    fs, roots = _object(tmp_path, 50)
    ok = validate_object(fs, "o", chain=ChainCheck(Chain(60, roots), 3))
    assert ok.is_valid and not [f for f in ok.findings if f.code in ("E906", "W903")]

    bad = validate_object(fs, "o", chain=ChainCheck(Chain(60, {}), 3))
    assert sorted(f.detail["proof"] for f in bad.errors if f.code == "E906") == [
        ots.inventory_proof_path("v1"),
        "v1/content/x.ots",
    ]

    shallow = validate_object(fs, "o", chain=ChainCheck(Chain(51, roots), 3))
    assert shallow.is_valid
    assert len([f for f in shallow.warnings if f.code == "W903"]) == 2

    results = ots.check_object_chain(fs, "o", ChainCheck(Chain(60, roots), 3))
    assert [(r.proof.kind, r.status) for r in results] == [
        ("content", AttestationStatus.VERIFIED),
        ("inventory", AttestationStatus.VERIFIED),
    ]
    assert results[0].verified is not None and results[0].verified.height == 50


def _client(handler, *, sleeps: list[float] | None = None) -> BitcoinRpcClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return BitcoinRpcClient(
        "http://node",
        client=http,
        sleep=(sleeps.append if sleeps is not None else lambda _s: None),
    )


def _ok(result) -> httpx.Response:
    return httpx.Response(200, json={"result": result, "error": None, "id": 0})


def test_block_at():
    root = bytes(range(32))

    def handler(request):
        method = json.loads(request.content)["method"]
        if method == "getblockhash":
            return _ok("ab" * 32)
        return _ok(
            {
                "hash": "ab" * 32,
                "height": 7,
                "confirmations": 4,
                "merkleroot": root[::-1].hex(),
                "time": 0,
            }
        )

    client = _client(handler)
    assert isinstance(client, ots.BlockHeaderSource)
    assert client.block_at(7) == ChainBlock(7, "ab" * 32, root, 4)


def test_block_at_missing_is_none():
    def handler(request):
        return httpx.Response(
            500, json={"result": None, "error": {"code": -8, "message": "out"}}
        )

    assert _client(handler).block_at(9) is None


@pytest.mark.parametrize(
    ("status", "exc", "calls"),
    [
        (401, BitcoinRpcAuthError, 1),
        (404, BitcoinRpcError, 1),
        (503, BitcoinRpcTransientError, 3),
    ],
)
def test_http_errors(status, exc, calls):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status)

    sleeps: list[float] = []
    with pytest.raises(exc):
        _client(handler, sleeps=sleeps).getblockhash(1)
    assert len(seen) == calls
    assert sleeps == [2.0, 4.0][: calls - 1]


def test_no_block_error_is_not_retried():
    def handler(request):
        return httpx.Response(
            500, json={"result": None, "error": {"code": -8, "message": "out"}}
        )

    with pytest.raises(BitcoinNoBlockError):
        _client(handler).getblockhash(5)


def test_credentials_scrubbed():
    def handler(request):
        return httpx.Response(401)

    client = BitcoinRpcClient.connect("http://node", user="u", password="secret")
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler), headers=client._client.headers
    )
    with pytest.raises(BitcoinRpcAuthError) as info:
        client.getblockhash(1)
    cause = info.value.__cause__
    assert isinstance(cause, httpx.HTTPStatusError)
    assert cause.request.headers["authorization"] == "<redacted>"
