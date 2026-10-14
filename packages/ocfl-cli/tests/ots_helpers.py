import hashlib
from collections.abc import Sequence
from pathlib import Path

import pytest
from fakes import FakeBitcoinRPC, FakeSimpleProof
from msgspec.structs import replace
from runner_helpers import ObjRunner, stage_args, staging_dir

from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app
from ocfl_cli.model.ots_state import Proof, VerifyJob
from ocfl_cli.model.phase import Phase, TimestampTarget
from ocfl_cli.ports import BitcoinRPC
from ocfl_cli.store.ots_state import load_ots, ots_cache_path, save_ots
from tests.helpers.ots import build_attestation

DIGEST_BLOCK_HEIGHT = 800_000
MIN_CONFS = 3
CATEGORY_ID = 641
UNRELATED_DIGEST = "de" * 32

SP_OK = f"""\
[simpleproof]
base_url = "https://stage.example"
proof_category_id = {CATEGORY_ID}
"""

BTC_OK = """\
[bitcoin]
rpc_url = "http://127.0.0.1:8332"
zmq_endpoint = "tcp://127.0.0.1:28332"
"""

VERSION_METADATA = (
    "-m",
    "Timestamped inventory",
    "--name",
    "Alice",
    "--address",
    "mailto:alice@example.org",
)


def write_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: str = SP_OK + BTC_OK,
) -> Path:
    cfg = tmp_path / "ocfl-config" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(body)
    cfg.chmod(0o600)
    monkeypatch.setenv("OCFL_CONFIG", str(cfg))
    return cfg


def inject_fake_sp(runner: ObjRunner, fake: FakeSimpleProof) -> None:
    runner.obj["sp_client_factory"] = lambda cfg: fake


def inject_fake_btc(runner: ObjRunner, fake: BitcoinRPC) -> None:
    runner.obj["btc_rpc_factory"] = lambda cfg: fake


def _grow_chain_to(rpc: FakeBitcoinRPC, height: int) -> None:
    needed = height - rpc.tip_height
    if needed > 0:
        rpc.extend(needed)


def bury(
    rpc: FakeBitcoinRPC, confs: int = MIN_CONFS, height: int = DIGEST_BLOCK_HEIGHT
) -> None:
    _grow_chain_to(rpc, height + confs - 1)


def end_chain_below(
    rpc: FakeBitcoinRPC, blocks: int, height: int = DIGEST_BLOCK_HEIGHT
) -> None:
    _grow_chain_to(rpc, height - blocks)


def replace_attested_block(
    rpc: FakeBitcoinRPC, *, confs: int, height: int = DIGEST_BLOCK_HEIGHT
) -> None:
    rpc.reorg_at(height)
    bury(rpc, confs, height)


def register_mismatched_merkle_root(
    rpc: FakeBitcoinRPC, height: int = DIGEST_BLOCK_HEIGHT
) -> None:
    rpc.register_merkle_root(height, "00" * 32)


def register_attested(
    rpc: FakeBitcoinRPC, digest_hex: str, height: int = DIGEST_BLOCK_HEIGHT
) -> None:
    # `build_attestation` has no ops, so the attested merkle root is the
    # digest itself; bitcoind reports merkle roots in display (reversed) hex.
    rpc.register_merkle_root(height, bytes.fromhex(digest_hex)[::-1].hex())


def verified_content_proof(digest_hex: str) -> Proof:
    return Proof(
        target=TimestampTarget.CONTENT,
        ots_path=f"ots-cache/{digest_hex}.ots",
        verified=True,
        block_height=DIGEST_BLOCK_HEIGHT,
        confirmations_at_verify=MIN_CONFS,
        verified_at="2026-04-23T16:30:00+00:00",
    )


def ots_for(digest_hex: str, *, block_height: int = DIGEST_BLOCK_HEIGHT) -> bytes:
    return build_attestation(bytes.fromhex(digest_hex), block_height=block_height)


def new_ots_staging(
    runner: ObjRunner,
    root: Path,
    staging: Path,
    object_id: str,
    *files: tuple[str, bytes],
    new_args: Sequence[str] = (),
) -> tuple[Path, dict[str, str]]:
    r = runner.invoke(
        app, stage_args(root, staging, "new", object_id, "--ots", *new_args)
    )
    assert r.exit_code == ExitCode.SUCCESS, r.output

    digests: dict[str, str] = {}
    for name, data in files:
        src = staging.parent / name
        src.write_bytes(data)
        r = runner.invoke(app, stage_args(root, staging, "add", object_id, str(src)))
        assert r.exit_code == ExitCode.SUCCESS, r.output
        digests[name] = hashlib.sha256(data).hexdigest()

    return staging_dir(root, staging, object_id), digests


def setup_at_inventory_pending(
    runner: ObjRunner,
    root: Path,
    staging: Path,
    object_id: str,
    sp_fake: FakeSimpleProof,
) -> tuple[Path, str, str]:
    """Returns ``(staging_dir, content_digest, inv_digest)``."""
    inject_fake_sp(runner, sp_fake)
    sd, digests = new_ots_staging(
        runner,
        root,
        staging,
        object_id,
        ("a.txt", b"alpha"),
        new_args=VERSION_METADATA,
    )
    da = digests["a.txt"]

    ots_src = staging.parent / "a.txt.ots"
    # A final proof of a.txt: the commit refuses an invalid or pending one.
    ots_src.write_bytes(
        build_attestation(
            hashlib.sha256(b"alpha").digest(), block_height=DIGEST_BLOCK_HEIGHT
        )
    )
    r = runner.invoke(app, stage_args(root, staging, "add", object_id, str(ots_src)))
    assert r.exit_code == ExitCode.SUCCESS, r.output

    state = load_ots(sd)
    state = replace(
        state,
        phase=Phase.CONTENT_UPGRADED,
        pending_digests=(da,),
        proofs={
            da: Proof(
                target=TimestampTarget.CONTENT,
                ots_path=f"ots-cache/{da}.ots",
                verified=True,
                block_height=DIGEST_BLOCK_HEIGHT,
                confirmations_at_verify=MIN_CONFS,
                verified_at="2026-04-23T16:30:00+00:00",
            )
        },
    )
    save_ots(sd, state)

    r = runner.invoke(
        app, stage_args(root, staging, "timestamp", object_id, "--expect", "inventory")
    )
    assert r.exit_code == ExitCode.SUCCESS, r.output

    state = load_ots(sd)
    assert state.phase is Phase.INVENTORY_PENDING
    assert state.inventory is not None
    assert state.inventory_proof is not None
    return sd, da, state.inventory.digest


def seed_inv_completed_verify_job(
    sp_fake: FakeSimpleProof,
    sd: Path,
    inv_digest: str,
    ots_bytes: bytes,
) -> str:
    job_id = sp_fake.verify_batch([inv_digest]).job_id
    sp_fake.advance_to_completed(job_id, {inv_digest: ots_bytes})

    state = load_ots(sd)
    assert state.inventory is not None
    assert state.inventory_proof is not None
    new_inv = replace(state.inventory, job_id=job_id)
    state = replace(
        state,
        inventory=new_inv,
        verify_jobs=(
            *state.verify_jobs,
            VerifyJob(job_id=job_id, digests=(inv_digest,), status="IN_PROGRESS"),
        ),
    )
    save_ots(sd, state)
    return job_id


def setup_at_inventory_upgraded(
    runner: ObjRunner,
    root: Path,
    staging: Path,
    object_id: str,
    sp_fake: FakeSimpleProof,
    btc_rpc: FakeBitcoinRPC,
) -> tuple[Path, str, str, bytes]:
    """Returns ``(staging_dir, content_digest, inv_digest, cached_ots_bytes)``."""
    inject_fake_btc(runner, btc_rpc)
    sd, da, inv_digest = setup_at_inventory_pending(
        runner, root, staging, object_id, sp_fake
    )
    ots_bytes = ots_for(inv_digest)
    seed_inv_completed_verify_job(sp_fake, sd, inv_digest, ots_bytes)
    bury(btc_rpc)
    register_attested(btc_rpc, inv_digest)

    r = runner.invoke(
        app,
        stage_args(root, staging, "upgrade", object_id, "--expect", "inventory"),
    )
    assert r.exit_code == ExitCode.SUCCESS, r.output
    state = load_ots(sd)
    assert state.phase is Phase.INVENTORY_UPGRADED
    assert ots_cache_path(sd, inv_digest).read_bytes() == ots_bytes
    return sd, da, inv_digest, ots_bytes
