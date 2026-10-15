import contextlib
import os
import secrets
import shutil
import socket
import subprocess
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2 as httpx


def find_bitcoind() -> str | None:
    explicit = os.environ.get("BITCOIND")
    if explicit:
        return explicit if Path(explicit).is_file() else None
    return shutil.which("bitcoind")


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass(frozen=True, slots=True)
class RegtestNode:
    rpc_url: str
    rpc_user: str
    rpc_password: str
    zmq_endpoint: str
    datadir: Path
    _proc: subprocess.Popen[bytes]
    _client: httpx.Client

    def rpc(self, method: str, params: list[Any] | None = None) -> Any:
        return _rpc_call(self._client, self.rpc_url, method, params)

    def wallet_rpc(
        self, wallet: str, method: str, params: list[Any] | None = None
    ) -> Any:
        return _rpc_call(
            self._client, f"{self.rpc_url}/wallet/{wallet}", method, params
        )


def _rpc_call(
    client: httpx.Client, url: str, method: str, params: list[Any] | None
) -> Any:
    """Bitcoin Core returns 500 with a JSON ``error`` body on RPC errors;
    surface those as ``RuntimeError`` rather than ``HTTPStatusError``."""
    payload = {"jsonrpc": "1.0", "id": 0, "method": method, "params": params or []}
    resp = client.post(url, json=payload)
    try:
        body = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise
    if body.get("error") is not None:
        raise RuntimeError(f"bitcoind RPC error ({method}): {body['error']}")
    if resp.status_code >= 400:
        resp.raise_for_status()
    return body["result"]


def start_regtest(bitcoind: str, datadir: Path) -> RegtestNode:
    rpc_port = _free_port()
    zmq_port = _free_port()
    rpc_user = "ocfltest"
    rpc_password = secrets.token_urlsafe(24)

    datadir.mkdir(parents=True, exist_ok=True)
    conf = datadir / "bitcoin.conf"
    conf.write_text(
        "regtest=1\n"
        "listen=0\n"
        "discover=0\n"
        "dnsseed=0\n"
        "upnp=0\n"
        "[regtest]\n"
        f"rpcuser={rpc_user}\n"
        f"rpcpassword={rpc_password}\n"
        "rpcbind=127.0.0.1\n"
        "rpcallowip=127.0.0.1\n"
        f"rpcport={rpc_port}\n"
        f"zmqpubhashblock=tcp://127.0.0.1:{zmq_port}\n"
        "fallbackfee=0.0001\n"
        "blockfilterindex=0\n"
        "txindex=0\n"
    )

    proc = subprocess.Popen(
        [
            bitcoind,
            f"-datadir={datadir}",
            f"-conf={conf}",
            "-noprinttoconsole",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    rpc_url = f"http://127.0.0.1:{rpc_port}"
    zmq_endpoint = f"tcp://127.0.0.1:{zmq_port}"
    client = httpx.Client(auth=(rpc_user, rpc_password), timeout=10.0)

    deadline = time.monotonic() + 30.0
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            client.close()
            raise RuntimeError(
                f"bitcoind exited early with code {proc.returncode}; "
                f"see {datadir / 'regtest' / 'debug.log'}"
            )
        try:
            resp = client.post(
                rpc_url,
                json={
                    "jsonrpc": "1.0",
                    "id": 0,
                    "method": "getblockchaininfo",
                    "params": [],
                },
            )
            if resp.status_code == 200 and resp.json().get("error") is None:
                break
            last_err = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
            last_err = exc
        time.sleep(0.2)
    else:
        client.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise RuntimeError(f"bitcoind did not become ready in 30s: {last_err}")

    return RegtestNode(
        rpc_url=rpc_url,
        rpc_user=rpc_user,
        rpc_password=rpc_password,
        zmq_endpoint=zmq_endpoint,
        datadir=datadir,
        _proc=proc,
        _client=client,
    )


def stop_regtest(node: RegtestNode) -> None:
    proc = node._proc
    client = node._client
    if proc.poll() is None:
        with contextlib.suppress(httpx.HTTPError):
            client.post(
                node.rpc_url,
                json={"jsonrpc": "1.0", "id": 0, "method": "stop", "params": []},
                timeout=5.0,
            )
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    client.close()


def ensure_wallet(node: RegtestNode, name: str = "ocfltest") -> str:
    existing = node.rpc("listwallets")
    if name in existing:
        return name
    try:
        node.rpc("loadwallet", [name])
    except RuntimeError:
        node.rpc("createwallet", [name])
    return name


COINBASE_MATURITY = 100


def fund_wallet(node: RegtestNode, wallet: str) -> None:
    addr = node.wallet_rpc(wallet, "getnewaddress")
    node.rpc("generatetoaddress", [COINBASE_MATURITY + 1, addr])


def serialized_block(node: RegtestNode, block_hash: str) -> bytes:
    hex_str = node.rpc("getblock", [block_hash, 0])
    return bytes.fromhex(hex_str)


def bitcoin_config_toml(node: RegtestNode, *, confirmations: int = 3) -> str:
    return (
        "[bitcoin]\n"
        f'rpc_url = "{node.rpc_url}"\n'
        f'zmq_endpoint = "{node.zmq_endpoint}"\n'
        f"confirmations = {confirmations}\n"
    )


def bury_to_confs(
    node: RegtestNode, height: int, target_confs: int, wallet: str
) -> None:
    addr = node.wallet_rpc(wallet, "getnewaddress")
    while True:
        info = node.rpc("getblockchaininfo")
        confs = int(info["blocks"]) - height + 1
        if confs >= target_confs:
            return
        node.rpc("generatetoaddress", [target_confs - confs, addr])


__all__ = [
    "RegtestNode",
    "bitcoin_config_toml",
    "bury_to_confs",
    "ensure_wallet",
    "find_bitcoind",
    "fund_wallet",
    "serialized_block",
    "start_regtest",
    "stop_regtest",
]
