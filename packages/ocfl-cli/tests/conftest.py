"""Shared CLI test fixtures."""

import json
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fakes import FakeBitcoinRPC, FakeSimpleProof, InMemoryHashBlockStream
from typer.testing import CliRunner

from ocfl_lib import DigestAlgorithm, LocalFS, Spec, StorageRoot, get_layout, stage_dir
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
    """Point `OCFL_CONFIG` at nonexistent path; tests must not read real user config."""
    sentinel = tmp_path_factory.mktemp("ocfl-config") / "config.toml"
    monkeypatch.setenv("OCFL_CONFIG", str(sentinel))


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def fixtures_root() -> Path:
    root = Path(__file__).resolve().parents[3] / "tests" / "fixtures"
    assert root.is_dir()
    return root


@pytest.fixture
def storage_root(tmp_path: Path) -> Path:
    """Create a valid storage root with one object for testing."""
    fs = LocalFS(tmp_path)
    layout = get_layout("0002-flat-direct-storage-layout")
    root = StorageRoot.init(fs, fs, spec=Spec.V1_1, layout=layout)

    # Create a test object with 2 versions
    obj_dir = tmp_path / "test-files"
    obj_dir.mkdir()
    (obj_dir / "file1.txt").write_text("hello")
    stage = stage_dir(fs, "test-files", DigestAlgorithm.SHA512)
    root.commit(fs, "obj:test", stage, message="v1")

    # v2
    (obj_dir / "file2.txt").write_text("world")
    stage2 = stage_dir(fs, "test-files", DigestAlgorithm.SHA512)
    root.commit(fs, "obj:test", stage2, message="v2")

    shutil.rmtree(obj_dir)

    return tmp_path


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
def staging_with_ots_json(tmp_path: Path) -> Path:
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "ots.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "phase": "drafting",
                "category_id": 641,
                "pending_digests": [],
                "submissions": [],
                "verify_jobs": [],
                "content_proofs": {},
                "inventory_proof": None,
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


@pytest.fixture
def real_ots_bytes(bitcoind_regtest: RegtestNode) -> bytes:
    """Real OTS attestation for ``b"hello regtest"`` against bitcoind regtest."""
    import hashlib

    from tests.helpers.ots import build_regtest_attestation

    digest = hashlib.sha256(b"hello regtest").digest()
    ots_bytes, _height, _block_hash = build_regtest_attestation(
        bitcoind_regtest, digest
    )
    return ots_bytes
