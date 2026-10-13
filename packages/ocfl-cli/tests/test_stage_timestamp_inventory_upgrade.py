"""Tests for `ocfl stage upgrade [--watch]` and `stage retry` on the inventory.

Pins step 15 of `OTS workflow contract` — the inventory-side mirror of
the content upgrade / watch / retry shape:

- ``upgrade``: single-digest (the inventory digest) verify pass with
  the same crash-recovery invariant as the content upgrade. Step (1)
  atomic cache write at ``ots-cache/<inv_digest>.ots``; step (2) ingest
  is VACUOUS for inventory (the extension-file entry is queued at
  commit time, not now); step (3) atomic ``ots.json`` update sets
  ``proofs[<inventory digest>] = Proof(target=INVENTORY, verified=True,
  block_height, confirmations_at_verify, verified_at,
  ots_path=ots-cache/<d>.ots)``
  and advances phase to ``inventory-upgraded``.
- ``upgrade --watch``: ZMQ-driven loop that runs an upgrade pass per Bitcoin
  block; exits 0 when phase advances to ``inventory-upgraded`` or 130
  on SIGINT/SIGTERM.
- ``retry``: re-POST the inventory digest at phase
  ``INVENTORY_PENDING``; ``PhaseError`` (exit 3) at any other phase.
"""

import hashlib
import json
import logging
import os
import shutil
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest
from fakes import FakeBitcoinRPC, FakeSimpleProof, RecordingSleeper
from fakes.locks import held_flock
from msgspec.structs import replace
from pydantic import SecretStr
from runner_helpers import runner_obj
from typer.testing import CliRunner

from ocfl_cli.config import BitcoinConfig, Config
from ocfl_cli.config.ots import SimpleProofConfig
from ocfl_cli.main import app
from ocfl_cli.model.ots_state import Proof, VerifyJob
from ocfl_cli.model.phase import Phase, TimestampTarget
from ocfl_cli.ports import BlockStream
from ocfl_cli.store.ots_state import load_ots, ots_cache_path, save_ots
from ocfl_cli.store.staging import StagingStore
from ocfl_cli.workflow import timestamp as wf_timestamp
from ocfl_cli.workflow.common import (
    compute_inventory_job_progress,
    poll_outstanding_jobs,
    snapshot_results,
)
from ocfl_lib import LocalFS, Spec, StorageRoot, get_layout
from tests.helpers.ots import build_attestation

# --------------------------------------------------------------------------- #
# Config + path helpers (DUPLICATED from test_stage_timestamp_content_upgrade.py
# / test_stage_timestamp_inventory.py per workflow rule
# "smaller file > shared module before 3rd use"; reviewer decides extraction).
# --------------------------------------------------------------------------- #

_SP_OK = """\
[simpleproof]
base_url = "https://stage.example"
proof_category_id = 641
"""

_BTC_OK_COOKIE = """\
[bitcoin]
rpc_url = "http://127.0.0.1:8332"
zmq_endpoint = "tcp://127.0.0.1:28332"
confirmations = 3
"""


def _write_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: str = _SP_OK + _BTC_OK_COOKIE,
) -> Path:
    cfg = tmp_path / "ocfl-config" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(body)
    monkeypatch.setenv("OCFL_CONFIG", str(cfg))
    return cfg


def _stage_args(root: Path, staging: Path, *args: str) -> list[str]:
    return ["--root", str(root), "stage", "--staging-root", str(staging), *args]


def _staging_dir(root: Path, staging: Path, object_id: str) -> Path:
    store = StagingStore(staging)
    return store.staging_path(str(root.resolve()), object_id)


def _inject_fake_sp(runner: CliRunner, fake: FakeSimpleProof) -> None:
    """Inject *fake* via ``runner.obj["sp_client_factory"]``."""
    runner_obj(runner)["sp_client_factory"] = lambda cfg: fake


def _inject_fake_btc(runner: CliRunner, fake: FakeBitcoinRPC) -> None:
    """Inject *fake* via ``runner.obj["btc_rpc_factory"]``."""
    runner_obj(runner)["btc_rpc_factory"] = lambda cfg: fake


def _inject_fake_zmq(runner: CliRunner, stream: BlockStream) -> None:

    runner_obj(runner)["block_stream_factory"] = lambda *a, **kw: stream


@pytest.fixture(autouse=True)
def no_block_processing_delay(runner: CliRunner) -> None:

    runner_obj(runner)["sleeper"] = RecordingSleeper()


# --------------------------------------------------------------------------- #
# Storage-root + .ots/chain helpers (copied from content-upgrade tests).
# --------------------------------------------------------------------------- #


@pytest.fixture
def empty_sha256_root(tmp_path: Path) -> Path:
    location = tmp_path / "root"
    location.mkdir()
    fs = LocalFS(location)
    layout = get_layout("0002-flat-direct-storage-layout")
    StorageRoot.init(fs, fs, spec=Spec.V1_1, layout=layout)
    return location


DIGEST_BLOCK_HEIGHT = 800_000
MIN_CONFS = 3


def _ots_for(digest_hex: str, *, block_height: int = DIGEST_BLOCK_HEIGHT) -> bytes:
    return build_attestation(bytes.fromhex(digest_hex), block_height=block_height)


def _ots_pending(digest_hex: str) -> bytes:
    return build_attestation(
        bytes.fromhex(digest_hex),
        pending_uri="https://a.pool.opentimestamps.org",
    )


def _grow_chain_to(rpc: FakeBitcoinRPC, height: int) -> None:
    needed = height - rpc.tip_height
    if needed > 0:
        rpc.extend(needed)


def _register_attested(
    rpc: FakeBitcoinRPC, digest_hex: str, height: int = DIGEST_BLOCK_HEIGHT
) -> None:
    rpc.register_merkle_root(height, bytes.fromhex(digest_hex)[::-1].hex())


# --------------------------------------------------------------------------- #
# Setup: bring an OTS staging to phase=inventory-pending.
#
# Strategy (mirrors test_stage_timestamp_inventory.py::_setup_content_upgraded):
#   1. ``stage new --ots`` + ``stage add a.txt`` + ``stage add a.txt.ots``
#   2. mutate ots.json to CONTENT_UPGRADED with a verified content_proof
#      so the FSM legality check at ``timestamp inventory`` passes
#   3. run ``stage timestamp inventory`` to land at INVENTORY_PENDING
#      (this populates inventory_proof + writes plan.json)
# --------------------------------------------------------------------------- #


def _new_ots_staging(
    runner: CliRunner,
    root: Path,
    staging: Path,
    object_id: str,
    *files: tuple[str, bytes],
) -> tuple[Path, dict[str, str], dict[str, str]]:
    r = runner.invoke(
        app,
        _stage_args(
            root,
            staging,
            "new",
            object_id,
            "--ots",
            "-m",
            "Timestamped inventory",
            "--name",
            "Alice",
            "--address",
            "mailto:alice@example.org",
        ),
    )
    assert r.exit_code == 0, r.output

    paths: dict[str, str] = {}
    digests: dict[str, str] = {}
    for name, data in files:
        src = staging.parent / name
        src.write_bytes(data)
        r = runner.invoke(app, _stage_args(root, staging, "add", object_id, str(src)))
        assert r.exit_code == 0, r.output
        paths[name] = name
        digests[name] = hashlib.sha256(data).hexdigest()

    return _staging_dir(root, staging, object_id), paths, digests


def _setup_at_inventory_pending(
    runner: CliRunner,
    root: Path,
    staging: Path,
    object_id: str,
) -> tuple[Path, str, str]:
    """Build a staging at phase=inventory-pending with a populated
    ``inventory_proof`` and a serialized ``plan.json``.

    Returns ``(staging_dir, content_digest, inv_digest)``.

    Caller MUST have already called ``_inject_fake_sp`` (the
    ``stage timestamp inventory`` invocation here POSTs the inv digest).
    """
    sd, _paths, digests = _new_ots_staging(
        runner, root, staging, object_id, ("a.txt", b"alpha")
    )
    da = digests["a.txt"]

    ots_src = staging.parent / "a.txt.ots"
    # A final proof of a.txt: the commit refuses an invalid or pending one.
    ots_src.write_bytes(
        build_attestation(
            hashlib.sha256(b"alpha").digest(), block_height=DIGEST_BLOCK_HEIGHT
        )
    )
    r = runner.invoke(app, _stage_args(root, staging, "add", object_id, str(ots_src)))
    assert r.exit_code == 0, r.output

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
        app, _stage_args(root, staging, "timestamp", object_id, "--expect", "inventory")
    )
    assert r.exit_code == 0, r.output

    state = load_ots(sd)
    assert state.phase is Phase.INVENTORY_PENDING
    assert state.inventory is not None
    assert state.inventory_proof is not None
    return sd, da, state.inventory.digest


def _seed_inv_completed_verify_job(
    sp_fake: FakeSimpleProof,
    sd: Path,
    inv_digest: str,
    ots_bytes: bytes,
) -> str:
    """Pre-create a COMPLETED job on the fake covering ``inv_digest``.

    Records the job in BOTH common storage locations so the implementer
    has latitude (per the spec's "implementer chooses the storage
    location" note):
      - ``state.verify_jobs`` (mirrors content --upgrade shape)
      - ``state.inventory.job_id`` (single-digest convenience)

    The upgrade pass should detect the existing job, poll, see
    COMPLETED, then download + verify.
    """
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


# =========================================================================== #
# Service primitives for batch-size-1 inventory upgrade
# =========================================================================== #


class TestInventoryUpgradePrimitives:
    def test_snapshot_inventory_progress_ignores_unconfirmed_result(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        runtime,
    ):
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        staging = tmp_path / "staging"
        sd, _content_digest, inv_digest = _setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-progress"
        )
        job_id = sp_fake.verify_batch([inv_digest]).job_id
        sp_fake.advance_to_processing(job_id, {inv_digest: ()})
        state = load_ots(sd)
        assert state.inventory is not None
        assert state.inventory_proof is not None
        state = replace(
            state,
            inventory=replace(state.inventory, job_id=job_id),
            verify_jobs=(
                *state.verify_jobs,
                VerifyJob(job_id=job_id, digests=(inv_digest,), status="IN_PROGRESS"),
            ),
        )
        save_ots(sd, state)
        state = poll_outstanding_jobs(state, sp_fake)

        state = snapshot_results(state, sd, sp_fake)

        progress = compute_inventory_job_progress(state, runtime=runtime)
        assert progress is not None
        assert progress.anchored_digests == 0
        assert progress.pending_digests == 1

    def test_prepare_poll_ingest_inventory_verification(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        runtime,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, _da, inv_digest = _setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-upgrade-primitive"
        )
        cfg = Config(
            simpleproof=SimpleProofConfig(
                base_url="https://stage.example",
                proof_category_id=641,
                access_token=SecretStr("tok"),
                api_key=SecretStr("key"),
            ),
            bitcoin=BitcoinConfig(
                rpc_url="http://127.0.0.1:8332",
                zmq_endpoint="tcp://127.0.0.1:28332",
                rpc_user="u",
                rpc_password=SecretStr("p"),
                confirmations=MIN_CONFS,
            ),
        )
        assert cfg.simpleproof is not None
        assert cfg.bitcoin is not None

        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS)
        _register_attested(btc_fake.rpc, inv_digest)

        run = wf_timestamp.TimestampRun(
            cfg=cfg,
            store=StagingStore(staging),
            sr=StorageRoot.open(LocalFS(empty_sha256_root)),
            root_path=str(empty_sha256_root.resolve()),
            object_id="obj:inv-upgrade-primitive",
            staging_dir=sd,
            sp_factory=lambda cfg: sp_fake,
            btc_factory=lambda cfg: btc_fake.rpc,
            runtime=runtime,
        )

        # Pass 1 (target from the phase): queue the verify job.
        first = wf_timestamp.upgrade_locked(run)
        assert first.target is TimestampTarget.INVENTORY
        assert first.phase is Phase.INVENTORY_PENDING
        state = load_ots(sd)
        assert len(state.verify_jobs) == 1
        job_id = state.verify_jobs[0].job_id

        # Pass 2: the completed job is downloaded, verified and advances.
        sp_fake.advance_to_completed(job_id, {inv_digest: _ots_for(inv_digest)})
        second = wf_timestamp.upgrade_locked(run)
        assert second.phase is Phase.INVENTORY_UPGRADED
        state = load_ots(sd)
        assert state.verify_jobs[0].status == "COMPLETED"

        assert state.phase is Phase.INVENTORY_UPGRADED
        assert state.inventory is not None
        assert state.inventory_proof is not None
        assert state.inventory_proof.verified
        assert ots_cache_path(sd, inv_digest).is_file()


# =========================================================================== #
# Layer-2: --upgrade single-digest happy path  (verifies + advances phase)
# =========================================================================== #


class TestInventoryUpgradeHappyPath:
    def test_inv_pending_advances_to_inventory_upgraded(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _da, inv_digest = _setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-up"
        )

        ots_bytes = _ots_for(inv_digest)
        _seed_inv_completed_verify_job(sp_fake, sd, inv_digest, ots_bytes)
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        _register_attested(btc_fake.rpc, inv_digest)

        # Snapshot stage-state paths/manifest before --upgrade — step (2)
        # is vacuous for inventory; nothing about staged content should
        # change as a result of the upgrade pass.
        store = StagingStore(staging)
        root_path = str(empty_sha256_root.resolve())
        pre_inv = store.load_inventory(root_path, "obj:inv-up")
        pre_paths = {
            p for ps in pre_inv.versions[pre_inv.head].state.values() for p in ps
        }
        pre_manifest_keys = set(pre_inv.manifest)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:inv-up",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == 0, r.output

        state = load_ots(sd)
        assert state.phase is Phase.INVENTORY_UPGRADED
        assert state.inventory is not None
        assert state.inventory_proof is not None
        proof = state.inventory_proof
        assert proof.verified is True
        assert proof.block_height == DIGEST_BLOCK_HEIGHT
        assert proof.block_hash == btc_fake.rpc.getblockhash(DIGEST_BLOCK_HEIGHT)
        assert proof.confirmations_at_verify is not None
        assert proof.confirmations_at_verify >= MIN_CONFS
        assert proof.verified_at is not None
        assert proof.ots_path == f"ots-cache/{inv_digest}.ots"

        # Cache bytes match what SP returned.
        assert ots_cache_path(sd, inv_digest).read_bytes() == ots_bytes

        # Step (2) vacuous: stage state's path-set + manifest unchanged
        # (extension-file entry is queued only at `stage commit`).
        post_inv = store.load_inventory(root_path, "obj:inv-up")
        post_paths = {
            p for ps in post_inv.versions[post_inv.head].state.values() for p in ps
        }
        assert post_paths == pre_paths
        assert set(post_inv.manifest) == pre_manifest_keys


class TestInventoryUpgradeExpired:
    def test_expired_verify_job_triggers_fresh_batch(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An EXPIRED verify job must not count as covering the inventory
        digest. The per-object --upgrade pass should submit a FRESH
        verify_batch (like the batch driver does) instead of stalling forever
        on the dead job. Guards the EXPIRED-as-active stall.
        """
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _da, inv_digest = _setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-exp"
        )

        # Seed an EXPIRED job covering the inventory digest.
        dead_id = sp_fake.verify_batch([inv_digest]).job_id
        sp_fake.expire_job(dead_id)
        state = load_ots(sd)
        assert state.inventory is not None
        assert state.inventory_proof is not None
        state = replace(
            state,
            inventory=replace(state.inventory, job_id=dead_id),
            verify_jobs=(
                *state.verify_jobs,
                VerifyJob(job_id=dead_id, digests=(inv_digest,), status="EXPIRED"),
            ),
        )
        save_ots(sd, state)
        assert len(sp_fake._jobs) == 1

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:inv-exp",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == 0, r.output
        # A fresh verify_batch was POSTed despite the EXPIRED job.
        assert len(sp_fake._jobs) == 2
        # State carries an active (non-terminal) job for the digest.
        state = load_ots(sd)
        assert any(
            inv_digest in j.digests
            and j.status not in {"FAILED", "EXPIRED", "COMPLETED"}
            for j in state.verify_jobs
        )


# =========================================================================== #
# Layer-2: inventory crash-recovery  (parametrize on step boundaries)
# =========================================================================== #


class TestInventoryCrashRecovery:
    """Inject a fault at each step boundary; assert restart converges.

    Boundaries tested (mirrors content --upgrade with step 2 vacuous):
      - ``before_cache``: fault before atomic cache write
      - ``after_cache_no_op``: ingest step is vacuous for inventory; no
        fault to install — assert single-pass convergence trivially
      - ``during_ots_save``: ots.json save raises mid-write (atomicity
        guards must keep the prior file readable)
    """

    @pytest.mark.parametrize(
        "fault_at",
        ["before_cache", "after_cache_no_op", "during_ots_save"],
    )
    def test_fault_at_boundary_converges_on_restart(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fault_at: str,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _da, inv_digest = _setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-crash"
        )

        ots_bytes = _ots_for(inv_digest)
        _seed_inv_completed_verify_job(sp_fake, sd, inv_digest, ots_bytes)
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        _register_attested(btc_fake.rpc, inv_digest)

        if fault_at != "after_cache_no_op":
            _install_inv_crash_fault(monkeypatch, fault_at, sd, inv_digest)

            # Round 1: expect non-zero exit (crash).
            r = runner.invoke(
                app,
                _stage_args(
                    empty_sha256_root,
                    staging,
                    "upgrade",
                    "obj:inv-crash",
                    "--expect",
                    "inventory",
                ),
            )
            assert r.exit_code != 0, r.output
            # Crash-recovery invariant: ots.json is never torn (always parses).
            post_crash = load_ots(sd)
            # If verified=True is recorded, cache bytes must already exist
            # (the implication direction of the three-step invariant).
            if (
                post_crash.inventory_proof is not None
                and post_crash.inventory_proof.verified is True
            ):
                assert ots_cache_path(sd, inv_digest).is_file()

            _uninstall_crash_fault(monkeypatch)

        # Round 2 (or sole pass): converges to inventory-upgraded.
        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:inv-crash",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == 0, r.output
        state = load_ots(sd)
        assert state.phase is Phase.INVENTORY_UPGRADED
        assert state.inventory is not None
        assert state.inventory_proof is not None
        assert state.inventory_proof.verified is True
        assert ots_cache_path(sd, inv_digest).read_bytes() == ots_bytes


_CRASH_FAULT_RESTORERS: list = []


def _patch_attr(target: object, attr: str, value: object) -> None:
    sentinel = object()
    original = getattr(target, attr, sentinel)
    setattr(target, attr, value)
    if original is sentinel:
        _CRASH_FAULT_RESTORERS.append(lambda: delattr(target, attr))
    else:
        _CRASH_FAULT_RESTORERS.append(lambda: setattr(target, attr, original))


def _install_inv_crash_fault(
    monkeypatch: pytest.MonkeyPatch,
    fault_at: str,
    sd: Path,
    inv_digest: str,
) -> None:
    """Crash at one boundary of the inventory upgrade's ingest.

    Every staging write ends in ``os.replace``; the fault fails the replace
    that would publish the file at the chosen boundary:

      - ``before_cache``: the inventory proof's ots-cache entry is never
        written.
      - ``during_ots_save``: saving the verified ``ots.json`` fails and a
        dead process's temp file is left next to it. The proof sits in
        ``proofs[<inventory digest>]``.
    """
    cache_path = ots_cache_path(sd, inv_digest)
    ots_json_path = sd / "ots.json"

    def verified_save(src: object) -> bool:
        try:
            parsed = json.loads(Path(str(src)).read_bytes())
        except OSError, json.JSONDecodeError:
            return False
        proofs = parsed.get("proofs") or {}
        ip = proofs.get(inv_digest) if isinstance(proofs, dict) else None
        return isinstance(ip, dict) and ip.get("verified") is True

    real_replace = os.replace

    def faulty_replace(src, dst, *args, **kwargs):
        dst_path = Path(dst)
        if fault_at == "before_cache" and dst_path == cache_path:
            raise OSError("injected: inventory cache write failed")
        if (
            fault_at == "during_ots_save"
            and dst_path == ots_json_path
            and verified_save(src)
        ):
            shutil.copyfile(src, dst_path.with_name(f"{dst_path.name}.tmp.1"))
            raise OSError("injected: crash mid-save (inventory)")
        return real_replace(src, dst, *args, **kwargs)

    if fault_at not in ("before_cache", "during_ots_save"):
        raise AssertionError(f"unknown fault_at: {fault_at!r}")
    _patch_attr(os, "replace", faulty_replace)


def _uninstall_crash_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    while _CRASH_FAULT_RESTORERS:
        _CRASH_FAULT_RESTORERS.pop()()


# =========================================================================== #
# Layer-2: phase advance to inventory-upgraded (verified / pending / shallow)
# =========================================================================== #


class TestInventoryPhaseAdvance:
    def test_advance_when_verified(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify success at min_confs ⇒ advance to inventory-upgraded."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _da, inv_digest = _setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-adv"
        )

        ots_bytes = _ots_for(inv_digest)
        _seed_inv_completed_verify_job(sp_fake, sd, inv_digest, ots_bytes)
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        _register_attested(btc_fake.rpc, inv_digest)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:inv-adv",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == 0, r.output
        state = load_ots(sd)
        assert state.phase is Phase.INVENTORY_UPGRADED
        assert state.inventory is not None
        assert state.inventory_proof is not None
        assert state.inventory_proof.verified is True

    def test_no_advance_when_pending_attestation_held(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """SP returns a PendingAttestation-only ``.ots`` ⇒ stays at
        inventory-pending with ``inventory_proof.proof.verified=False``;
        cache bytes still written (step 1)."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _da, inv_digest = _setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-pend"
        )

        pending_bytes = _ots_pending(inv_digest)
        _seed_inv_completed_verify_job(sp_fake, sd, inv_digest, pending_bytes)
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:inv-pend",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == 0, r.output
        state = load_ots(sd)
        assert state.phase is Phase.INVENTORY_PENDING
        assert state.inventory is not None
        assert state.inventory_proof is not None
        assert state.inventory_proof.verified is False
        # Cache bytes still written even on pending attestation.
        assert ots_cache_path(sd, inv_digest).is_file()

    def test_no_advance_below_min_confirmations(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Chain shallower than ``confirmations`` past attested height ⇒
        stays at inventory-pending; ``verified=False`` persisted."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _da, inv_digest = _setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-shallow"
        )

        ots_bytes = _ots_for(inv_digest)
        _seed_inv_completed_verify_job(sp_fake, sd, inv_digest, ots_bytes)
        # Tip only 1 confirmation past attested height.
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT)
        _register_attested(btc_fake.rpc, inv_digest)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:inv-shallow",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == 0, r.output
        state = load_ots(sd)
        assert state.phase is Phase.INVENTORY_PENDING
        assert state.inventory is not None
        assert state.inventory_proof is not None
        assert state.inventory_proof.verified is False


# =========================================================================== #
# Layer-2: --watch loop for inventory
# =========================================================================== #


class TestInventoryWatchLoop:
    def test_watch_sleeps_after_block_before_upgrade(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _da, inv_digest = _setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-watch-delay"
        )

        ots_bytes = _ots_for(inv_digest)
        _seed_inv_completed_verify_job(sp_fake, sd, inv_digest, ots_bytes)
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        _register_attested(btc_fake.rpc, inv_digest)

        class _OneBlockStream:
            def __init__(self) -> None:
                self._n = 0

            def wait_for_block(self, timeout_ms: int) -> bytes | None:
                self._n += 1
                if self._n == 1:
                    return bytes.fromhex(
                        btc_fake.rpc.getblockchaininfo().best_block_hash
                    )
                raise KeyboardInterrupt("loop did not exit after phase advance")

        _inject_fake_zmq(runner, _OneBlockStream())

        calls: list[str] = []

        runner_obj(runner)["sleeper"] = SimpleNamespace(
            sleep=lambda _seconds: calls.append("sleep")
        )

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:inv-watch-delay",
                "--watch",
                "--expect",
                "inventory",
            ),
        )

        assert r.exit_code == 0, r.output
        assert calls == ["sleep"]

    def test_watch_advances_on_block(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One pre-pushed block ⇒ one upgrade pass that verifies + advances
        phase to inventory-upgraded; exits 0."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _da, inv_digest = _setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-watch-ok"
        )

        ots_bytes = _ots_for(inv_digest)
        _seed_inv_completed_verify_job(sp_fake, sd, inv_digest, ots_bytes)
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        _register_attested(btc_fake.rpc, inv_digest)

        class _OneBlockStream:
            """Deliver one block then KeyboardInterrupt as defensive
            terminator (the loop should exit on phase advance first)."""

            def __init__(self) -> None:
                self._n = 0

            def wait_for_block(self, timeout_ms: int) -> bytes | None:
                self._n += 1
                if self._n == 1:
                    return bytes.fromhex(
                        btc_fake.rpc.getblockchaininfo().best_block_hash
                    )
                raise KeyboardInterrupt("loop did not exit after phase advance")

        _inject_fake_zmq(runner, _OneBlockStream())

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:inv-watch-ok",
                "--watch",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == 0, r.output
        assert "Watch:" in r.output
        assert "Object: obj:inv-watch-ok" in r.output
        assert "Waiting for Bitcoin blocks" in r.output
        assert "Block:" in r.output
        assert "Height:" in r.output
        assert "Checking SimpleProof jobs" in r.output
        assert "Phase: inventory-pending" in r.output
        assert "Workflow:" in r.output
        assert "Phase: inventory-pending -> inventory-upgraded" in r.output
        assert "Next: ocfl stage commit obj:inv-watch-ok" in r.output
        assert load_ots(sd).phase is Phase.INVENTORY_UPGRADED

    def test_watch_sigint_exits_130_without_torn_checkpoint(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stream raises ``KeyboardInterrupt`` before delivering a block;
        CLI must exit 130 and ``ots.json`` must parse cleanly afterwards."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _da, inv_digest = _setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-watch-sigint"
        )

        ots_bytes = _ots_for(inv_digest)
        _seed_inv_completed_verify_job(sp_fake, sd, inv_digest, ots_bytes)
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        _register_attested(btc_fake.rpc, inv_digest)

        class _SigintStream:
            def wait_for_block(self, timeout_ms: int) -> bytes | None:
                raise KeyboardInterrupt("simulated SIGINT before first block")

        _inject_fake_zmq(runner, _SigintStream())

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:inv-watch-sigint",
                "--watch",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == 130, r.output
        # No torn checkpoint: ots.json parses cleanly after SIGINT.
        load_ots(sd)

    def test_watch_lock_timeout_warning_and_continue(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Pre-acquire the staging ``.lock``; first wake delivers a block
        (which hits ``LockTimeoutError`` ⇒ WARNING + continue); second
        wake raises KeyboardInterrupt to terminate (exit 130). Assert the
        loop did NOT crash with ``LockTimeoutError`` and that a WARNING
        was logged with a message mentioning ``lock``."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        # Speed knob: shrink the watch loop's lock timeout if the constant
        # exists (it does — see test_stage_timestamp_content_upgrade_watch).
        runner_obj(runner)["lock_timeout"] = 1

        staging = tmp_path / "staging"
        sd, _da, inv_digest = _setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-watch-lock"
        )

        ots_bytes = _ots_for(inv_digest)
        _seed_inv_completed_verify_job(sp_fake, sd, inv_digest, ots_bytes)
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        _register_attested(btc_fake.rpc, inv_digest)

        # Hold the staging .lock as another process would.
        sibling = ExitStack()
        sibling.enter_context(held_flock(sd / ".lock"))

        class _ContendedStream:
            def __init__(self) -> None:
                self._n = 0

            def wait_for_block(self, timeout_ms: int) -> bytes | None:
                self._n += 1
                if self._n == 1:
                    return bytes.fromhex(
                        btc_fake.rpc.getblockchaininfo().best_block_hash
                    )
                raise KeyboardInterrupt("terminate after lock-contention pass")

        _inject_fake_zmq(runner, _ContendedStream())

        try:
            with caplog.at_level(logging.WARNING):
                r = runner.invoke(
                    app,
                    _stage_args(
                        empty_sha256_root,
                        staging,
                        "upgrade",
                        "obj:inv-watch-lock",
                        "--watch",
                        "--expect",
                        "inventory",
                    ),
                )
        finally:
            sibling.close()

        assert r.exit_code == 130, r.output
        # No torn checkpoint after lock contention + cancel.
        load_ots(sd)
        warn_msgs = [
            rec.getMessage().lower()
            for rec in caplog.records
            if rec.levelno == logging.WARNING
        ]
        assert any("lock" in m for m in warn_msgs), warn_msgs


class TestInventoryRetryPhaseGate:
    @pytest.mark.parametrize(
        "phase",
        [
            Phase.DRAFTING,
            Phase.CONTENT_PENDING,
            Phase.CONTENT_UPGRADED,
            Phase.INVENTORY_UPGRADED,
        ],
    )
    def test_retry_at_wrong_phase_raises_phase_error(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        phase: Phase,
    ) -> None:
        """All phases ∉ {inventory-pending} ⇒ exit 3 with the wrong-phase
        value surfaced in the diagnostic."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, _da, _inv_digest = _setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-retry-phase"
        )
        # Mutate phase to the target (inventory_proof remains populated;
        # the phase gate is what the test exercises).
        save_ots(sd, replace(load_ots(sd), phase=phase))

        pre_calls = len(sp_fake._submissions)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "retry",
                "obj:inv-retry-phase",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == 3, r.output
        assert phase.value in r.output, r.output
        # No SP POSTs on rejection.
        assert len(sp_fake._submissions) == pre_calls


# =========================================================================== #
# Layer-2: --retry happy path  (re-POST inv_digest at INVENTORY_PENDING)
# =========================================================================== #


class TestInventoryRetryHappyPath:
    def _retry(self, runner: CliRunner, root: Path, staging: Path, oid: str):
        return runner.invoke(
            app,
            _stage_args(root, staging, "retry", oid, "--expect", "inventory"),
        )

    def test_retry_resends_a_failed_inventory_submission(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--retry`` re-POSTs the target's ``failed`` submissions, the same
        rule as content: the inventory submission is a ``Submission`` too."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, _da, inv_digest = _setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-retry-ok"
        )
        state = load_ots(sd)
        subs = tuple(
            replace(s, status="failed") if s.target == "inventory" else s
            for s in state.submissions
        )
        save_ots(sd, replace(state, submissions=subs))
        pre_calls = len(sp_fake._submissions)

        r = self._retry(runner, empty_sha256_root, staging, "obj:inv-retry-ok")
        assert r.exit_code == 0, r.output

        new_calls = sp_fake._submissions[pre_calls:]
        assert [tuple(c.digests) for c in new_calls] == [(inv_digest,)]
        state = load_ots(sd)
        assert state.phase is Phase.INVENTORY_PENDING
        (sub,) = [s for s in state.submissions if s.target == "inventory"]
        assert sub.status == "submitted"
        assert sub.attempts == 2

    def test_retry_without_a_failed_submission_sends_nothing(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No ``failed`` inventory submission: nothing is re-POSTed (it used
        to always re-POST the digest); the command says so and exits 0."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, _da, _inv_digest = _setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-retry-none"
        )
        before = (sd / "ots.json").read_bytes()
        pre_calls = len(sp_fake._submissions)

        r = self._retry(runner, empty_sha256_root, staging, "obj:inv-retry-none")
        assert r.exit_code == 0, r.output
        assert "no failed submissions to retry" in r.output
        assert len(sp_fake._submissions) == pre_calls
        assert (sd / "ots.json").read_bytes() == before


# =========================================================================== #
# Layer-3: regtest end-to-end inventory upgrade (skipped without --run-bitcoin)
# =========================================================================== #


@pytest.mark.bitcoin
class TestRegtestEndToEndInventoryUpgrade:
    def test_real_rpc_real_attestation_inventory(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        bitcoind_regtest,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Real RPC, real OTS bytes for the inventory digest; ``--upgrade``
        verifies + advances phase to ``inventory-upgraded``."""
        from tests.helpers.ots import build_regtest_attestation
        from tests.helpers.regtest import bitcoin_config_toml, bury_to_confs

        cfg_body = _SP_OK + bitcoin_config_toml(bitcoind_regtest, confirmations=3)
        monkeypatch.setenv("BITCOIN_RPC_USER", bitcoind_regtest.rpc_user)
        monkeypatch.setenv("BITCOIN_RPC_PASSWORD", bitcoind_regtest.rpc_password)
        _write_config(tmp_path, monkeypatch, body=cfg_body)
        _inject_fake_sp(runner, sp_fake)
        # Production btc_rpc_factory path used.

        staging = tmp_path / "staging"
        sd, _da, inv_digest = _setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:rt-inv"
        )

        ots_bytes, attest_height, _ = build_regtest_attestation(
            bitcoind_regtest, bytes.fromhex(inv_digest)
        )
        _seed_inv_completed_verify_job(sp_fake, sd, inv_digest, ots_bytes)
        bury_to_confs(bitcoind_regtest, attest_height, MIN_CONFS, "ocfltest")

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:rt-inv",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == 0, r.output

        state = load_ots(sd)
        assert state.phase is Phase.INVENTORY_UPGRADED
        assert state.inventory is not None
        assert state.inventory_proof is not None
        proof = state.inventory_proof
        assert proof.verified is True
        assert proof.block_height == attest_height
        assert proof.confirmations_at_verify is not None
        assert proof.confirmations_at_verify >= MIN_CONFS
        assert ots_cache_path(sd, inv_digest).read_bytes() == ots_bytes
