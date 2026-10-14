import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fakes import (
    FakeBitcoinRPC,
    FakeSimpleProof,
    InMemoryHashBlockStream,
    RecordingSleeper,
)
from seed_root import build_seed_root
from typer.testing import CliRunner

from ocfl_cli.ports import Runtime
from ocfl_cli.system import SYSTEM_CLOCK
from ocfl_lib import (
    LocalFS,
    Spec,
    StorageRoot,
    get_layout,
)
from tests.helpers.regtest import (
    RegtestNode,
    ensure_wallet,
    find_bitcoind,
    fund_wallet,
    start_regtest,
    stop_regtest,
)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-bitcoin",
        action="store_true",
        default=False,
        help="Run tests requiring a bitcoind regtest subprocess.",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--run-bitcoin"):
        return
    skip = pytest.mark.skip(reason="need --run-bitcoin")
    for item in items:
        if "bitcoin" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path_factory, monkeypatch):
    base = tmp_path_factory.mktemp("ocfl-config")
    sentinel = base / "config.toml"
    env_file = base / ".env"
    env_file.write_text(
        "SIMPLEPROOF_ACCESS_TOKEN=tok\n"
        "SIMPLEPROOF_API_KEY=key\n"
        "BITCOIN_RPC_USER=u\n"
        "BITCOIN_RPC_PASSWORD=p\n"
    )
    env_file.chmod(0o600)
    monkeypatch.setenv("OCFL_CONFIG", str(sentinel))
    monkeypatch.setenv("OCFL_ENV_FILE", str(env_file))


@pytest.fixture
def runner():
    """CliRunner that auto-attaches a per-test ``obj`` dict to every invoke.

    Tests inject `sp_client_factory` / `btc_rpc_factory` into ``runner.obj``
    (typically via per-file `_inject_fake_*` helpers); the wrapper passes
    the dict through to ``CliRunner.invoke(obj=...)``. Explicit ``obj=``
    kwargs from callers win.
    """
    runner = CliRunner()
    obj: dict = {}
    runner.obj = obj  # ty: ignore[unresolved-attribute]
    original_invoke = runner.invoke

    def invoke(*args, **kwargs):
        kwargs.setdefault("obj", obj)
        return original_invoke(*args, **kwargs)

    runner.invoke = invoke  # ty: ignore[invalid-assignment]
    return runner


@pytest.fixture
def fixtures_root() -> Path:
    root = Path(__file__).resolve().parents[3] / "tests" / "fixtures"
    assert root.is_dir()
    return root


@pytest.fixture
def storage_root(tmp_path: Path) -> Path:
    build_seed_root(tmp_path)
    return tmp_path


@pytest.fixture
def empty_sha256_root(tmp_path: Path) -> Path:
    location = tmp_path / "root"
    location.mkdir()
    fs = LocalFS(location)
    layout = get_layout("0002-flat-direct-storage-layout")
    StorageRoot.init(fs, fs, spec=Spec.V1_1, layout=layout)
    return location


@dataclass
class BtcFake:
    rpc: FakeBitcoinRPC
    stream: InMemoryHashBlockStream


@pytest.fixture
def sp_fake() -> FakeSimpleProof:
    return FakeSimpleProof()


@pytest.fixture
def btc_fake() -> BtcFake:
    return BtcFake(rpc=FakeBitcoinRPC(), stream=InMemoryHashBlockStream())


@pytest.fixture
def sleeper() -> RecordingSleeper:
    return RecordingSleeper()


@pytest.fixture
def runtime(sleeper: RecordingSleeper) -> Runtime:
    return Runtime(
        clock=SYSTEM_CLOCK,
        sleeper=sleeper,
        block_streams=lambda cfg: InMemoryHashBlockStream(),
    )


@pytest.fixture
def staging_with_ots_json(tmp_path: Path) -> Path:
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "ots.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "phase": "drafting",
                "category_id": 641,
                "pending_digests": [],
                "submissions": [],
                "verify_jobs": [],
                "proofs": {},
                "inventory": None,
            }
        )
    )
    return staging


@pytest.fixture(scope="session")
def bitcoind_regtest(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RegtestNode]:
    binary = find_bitcoind()
    if binary is None:
        pytest.skip("bitcoind not installed (set $BITCOIND or install bitcoin-core)")
    datadir = tmp_path_factory.mktemp("bitcoind-regtest", numbered=False)
    node = start_regtest(binary, datadir)
    try:
        ensure_wallet(node)
        fund_wallet(node, "ocfltest", blocks=101)
        yield node
    finally:
        stop_regtest(node)
