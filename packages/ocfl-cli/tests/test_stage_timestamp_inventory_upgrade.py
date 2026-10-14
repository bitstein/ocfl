import logging
import os
from contextlib import ExitStack
from pathlib import Path

import pytest
from fakes import FakeSimpleProof, RecordingSleeper
from fakes.locks import held_flock
from msgspec.structs import replace
from ots_helpers import (
    DIGEST_BLOCK_HEIGHT,
    MIN_CONFS,
    SP_OK,
    bury,
    crash_at,
    inject_fake_btc,
    ots_for,
    register_attested,
    seed_inv_completed_verify_job,
    setup_at_inventory_pending,
    write_config,
)
from pydantic import SecretStr
from runner_helpers import ObjRunner, stage_args

from ocfl_cli.config import BitcoinConfig, Config
from ocfl_cli.config.ots import SimpleProofConfig
from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app
from ocfl_cli.model.ots_state import VerifyJob
from ocfl_cli.model.phase import Phase, TimestampTarget
from ocfl_cli.ports import BlockStream
from ocfl_cli.store.ots_state import load_ots, ots_cache_path, save_ots, with_lock
from ocfl_cli.store.staging import StagingStore
from ocfl_cli.workflow import timestamp as wf_timestamp
from ocfl_cli.workflow.common import (
    compute_inventory_job_progress,
    poll_outstanding_jobs,
    snapshot_results,
)
from ocfl_lib import LocalFS, StorageRoot
from tests.helpers.faults import Crash, CrashBoundary
from tests.helpers.ots import build_attestation


def _inject_fake_zmq(runner: ObjRunner, stream: BlockStream) -> None:

    runner.ports.block_stream_factory = lambda *a, **kw: stream


@pytest.fixture(autouse=True)
def no_block_processing_delay(runner: ObjRunner) -> None:

    runner.ports.sleeper = RecordingSleeper()


def _ots_pending(digest_hex: str) -> bytes:
    return build_attestation(
        bytes.fromhex(digest_hex),
        pending_uri="https://a.pool.opentimestamps.org",
    )


class TestInventoryUpgradePrimitives:
    def test_snapshot_inventory_progress_ignores_unconfirmed_result(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        runtime,
    ):
        write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, _content_digest, inv_digest = setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-progress", sp_fake
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
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        runtime,
    ) -> None:
        write_config(tmp_path, monkeypatch)

        staging = tmp_path / "staging"
        sd, _da, inv_digest = setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-upgrade-primitive", sp_fake
        )
        cfg = Config(
            simpleproof_section=SimpleProofConfig(
                base_url="https://stage.example",
                proof_category_id=641,
                access_token=SecretStr("tok"),
                api_key=SecretStr("key"),
            ),
            bitcoin_section=BitcoinConfig(
                rpc_url="http://127.0.0.1:8332",
                zmq_endpoint="tcp://127.0.0.1:28332",
                rpc_user="u",
                rpc_password=SecretStr("p"),
                confirmations=MIN_CONFS,
            ),
        )
        assert cfg.simpleproof is not None
        assert cfg.bitcoin is not None

        bury(btc_fake.rpc)
        register_attested(btc_fake.rpc, inv_digest)

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

        with with_lock(sd, runtime) as locked:
            first = wf_timestamp.upgrade_locked(run, locked)
        assert first.target is TimestampTarget.INVENTORY
        assert first.phase is Phase.INVENTORY_PENDING
        state = load_ots(sd)
        assert len(state.verify_jobs) == 1
        job_id = state.verify_jobs[0].job_id

        sp_fake.advance_to_completed(job_id, {inv_digest: ots_for(inv_digest)})
        with with_lock(sd, runtime) as locked:
            second = wf_timestamp.upgrade_locked(run, locked)
        assert second.phase is Phase.INVENTORY_UPGRADED
        state = load_ots(sd)
        assert state.verify_jobs[0].status == "COMPLETED"

        assert state.phase is Phase.INVENTORY_UPGRADED
        assert state.inventory is not None
        assert state.inventory_proof is not None
        assert state.inventory_proof.verified
        assert ots_cache_path(sd, inv_digest).is_file()


class TestInventoryUpgradeHappyPath:
    def test_inv_pending_advances_to_inventory_upgraded(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _da, inv_digest = setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-up", sp_fake
        )

        ots_bytes = ots_for(inv_digest)
        seed_inv_completed_verify_job(sp_fake, sd, inv_digest, ots_bytes)
        bury(btc_fake.rpc)
        register_attested(btc_fake.rpc, inv_digest)

        store = StagingStore(staging)
        root_path = str(empty_sha256_root.resolve())
        pre_inv = store.load_inventory(root_path, "obj:inv-up")
        pre_paths = {
            p for ps in pre_inv.versions[pre_inv.head].state.values() for p in ps
        }
        pre_manifest_keys = set(pre_inv.manifest)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:inv-up",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output

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

        assert ots_cache_path(sd, inv_digest).read_bytes() == ots_bytes

        post_inv = store.load_inventory(root_path, "obj:inv-up")
        post_paths = {
            p for ps in post_inv.versions[post_inv.head].state.values() for p in ps
        }
        assert post_paths == pre_paths
        assert set(post_inv.manifest) == pre_manifest_keys


class TestInventoryUpgradeExpired:
    def test_expired_verify_job_triggers_fresh_batch(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _da, inv_digest = setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-exp", sp_fake
        )

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
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:inv-exp",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == ExitCode.PENDING, r.output
        assert len(sp_fake._jobs) == 2
        state = load_ots(sd)
        assert any(
            inv_digest in j.digests
            and j.status not in {"FAILED", "EXPIRED", "COMPLETED"}
            for j in state.verify_jobs
        )


class TestInventoryCrashRecovery:
    @pytest.mark.parametrize(
        ("boundary", "cached"),
        [("cache", False), ("proof", True), ("proof_torn", True)],
    )
    def test_crash_at_boundary_heals_on_next_pass(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        boundary: str,
        cached: bool,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _da, inv_digest = setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-crash", sp_fake
        )

        ots_bytes = ots_for(inv_digest)
        seed_inv_completed_verify_job(sp_fake, sd, inv_digest, ots_bytes)
        bury(btc_fake.rpc)
        register_attested(btc_fake.rpc, inv_digest)
        upgrade = stage_args(
            empty_sha256_root,
            staging,
            "upgrade",
            "obj:inv-crash",
            "--expect",
            "inventory",
        )

        with (
            CrashBoundary(os, "replace", crash_at(boundary, sd, inv_digest)),
            pytest.raises(Crash),
        ):
            runner.invoke(app, upgrade)

        state = load_ots(sd)
        assert state.phase is Phase.INVENTORY_PENDING
        assert ots_cache_path(sd, inv_digest).is_file() is cached
        assert not state.verified(inv_digest)

        r = runner.invoke(app, upgrade)
        assert r.exit_code == ExitCode.SUCCESS, r.output
        state = load_ots(sd)
        assert state.phase is Phase.INVENTORY_UPGRADED
        assert state.inventory is not None
        assert state.inventory_proof is not None
        assert state.inventory_proof.verified is True
        assert ots_cache_path(sd, inv_digest).read_bytes() == ots_bytes


class TestInventoryPhaseAdvance:
    def test_advance_when_verified(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _da, inv_digest = setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-adv", sp_fake
        )

        ots_bytes = ots_for(inv_digest)
        seed_inv_completed_verify_job(sp_fake, sd, inv_digest, ots_bytes)
        bury(btc_fake.rpc)
        register_attested(btc_fake.rpc, inv_digest)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:inv-adv",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        state = load_ots(sd)
        assert state.phase is Phase.INVENTORY_UPGRADED
        assert state.inventory is not None
        assert state.inventory_proof is not None
        assert state.inventory_proof.verified is True

    def test_no_advance_when_pending_attestation_held(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _da, inv_digest = setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-pend", sp_fake
        )

        pending_bytes = _ots_pending(inv_digest)
        seed_inv_completed_verify_job(sp_fake, sd, inv_digest, pending_bytes)
        bury(btc_fake.rpc)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:inv-pend",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == ExitCode.PENDING, r.output
        state = load_ots(sd)
        assert state.phase is Phase.INVENTORY_PENDING
        assert state.inventory is not None
        assert state.inventory_proof is not None
        assert state.inventory_proof.verified is False
        assert ots_cache_path(sd, inv_digest).is_file()

    def test_no_advance_below_min_confirmations(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _da, inv_digest = setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-shallow", sp_fake
        )

        ots_bytes = ots_for(inv_digest)
        seed_inv_completed_verify_job(sp_fake, sd, inv_digest, ots_bytes)
        bury(btc_fake.rpc, 1)
        register_attested(btc_fake.rpc, inv_digest)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:inv-shallow",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == ExitCode.PENDING, r.output
        state = load_ots(sd)
        assert state.phase is Phase.INVENTORY_PENDING
        assert state.inventory is not None
        assert state.inventory_proof is not None
        assert state.inventory_proof.verified is False


class TestInventoryWatchLoop:
    def test_watch_sleeps_after_block_before_upgrade(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _da, inv_digest = setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-watch-delay", sp_fake
        )

        ots_bytes = ots_for(inv_digest)
        seed_inv_completed_verify_job(sp_fake, sd, inv_digest, ots_bytes)
        bury(btc_fake.rpc, MIN_CONFS - 1)
        register_attested(btc_fake.rpc, inv_digest)

        class _OneBlockStream:
            def __init__(self) -> None:
                self._n = 0

            def wait_for_block(self, timeout_ms: int) -> bytes | None:
                self._n += 1
                if self._n == 1:
                    btc_fake.rpc.extend(1)
                    return bytes.fromhex(
                        btc_fake.rpc.getblockchaininfo().best_block_hash
                    )
                raise KeyboardInterrupt("loop did not exit after phase advance")

        _inject_fake_zmq(runner, _OneBlockStream())

        sleeper = RecordingSleeper()
        runner.ports.sleeper = sleeper

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:inv-watch-delay",
                "--watch",
                "--expect",
                "inventory",
            ),
        )

        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert len(sleeper.calls) == 1

    def test_watch_advances_on_block(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _da, inv_digest = setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-watch-ok", sp_fake
        )

        ots_bytes = ots_for(inv_digest)
        seed_inv_completed_verify_job(sp_fake, sd, inv_digest, ots_bytes)
        bury(btc_fake.rpc, MIN_CONFS - 1)
        register_attested(btc_fake.rpc, inv_digest)

        class _OneBlockStream:
            def __init__(self) -> None:
                self._n = 0

            def wait_for_block(self, timeout_ms: int) -> bytes | None:
                self._n += 1
                if self._n == 1:
                    btc_fake.rpc.extend(1)
                    return bytes.fromhex(
                        btc_fake.rpc.getblockchaininfo().best_block_hash
                    )
                raise KeyboardInterrupt("loop did not exit after phase advance")

        _inject_fake_zmq(runner, _OneBlockStream())

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:inv-watch-ok",
                "--watch",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
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
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _da, inv_digest = setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-watch-sigint", sp_fake
        )

        ots_bytes = ots_for(inv_digest)
        seed_inv_completed_verify_job(sp_fake, sd, inv_digest, ots_bytes)
        bury(btc_fake.rpc, MIN_CONFS - 1)
        register_attested(btc_fake.rpc, inv_digest)

        class _SigintStream:
            def wait_for_block(self, timeout_ms: int) -> bytes | None:
                raise KeyboardInterrupt("simulated SIGINT before first block")

        _inject_fake_zmq(runner, _SigintStream())

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:inv-watch-sigint",
                "--watch",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == ExitCode.INTERRUPTED, r.output
        load_ots(sd)

    def test_watch_lock_timeout_warning_and_continue(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_btc(runner, btc_fake.rpc)

        runner.ports.lock_timeout = 1

        staging = tmp_path / "staging"
        sd, _da, inv_digest = setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-watch-lock", sp_fake
        )

        ots_bytes = ots_for(inv_digest)
        seed_inv_completed_verify_job(sp_fake, sd, inv_digest, ots_bytes)
        bury(btc_fake.rpc)
        register_attested(btc_fake.rpc, inv_digest)

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
                    stage_args(
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

        assert r.exit_code == ExitCode.INTERRUPTED, r.output
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
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        phase: Phase,
    ) -> None:
        write_config(tmp_path, monkeypatch)

        staging = tmp_path / "staging"
        sd, _da, _inv_digest = setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-retry-phase", sp_fake
        )
        save_ots(sd, replace(load_ots(sd), phase=phase))

        pre_calls = len(sp_fake._submissions)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "retry",
                "obj:inv-retry-phase",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == ExitCode.PHASE, r.output
        assert phase.value in r.output, r.output
        assert len(sp_fake._submissions) == pre_calls


class TestInventoryRetryHappyPath:
    def _retry(self, runner: ObjRunner, root: Path, staging: Path, oid: str):
        return runner.invoke(
            app,
            stage_args(root, staging, "retry", oid, "--expect", "inventory"),
        )

    def test_retry_resends_a_failed_inventory_submission(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)

        staging = tmp_path / "staging"
        sd, _da, inv_digest = setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-retry-ok", sp_fake
        )
        state = load_ots(sd)
        subs = tuple(
            replace(s, status="failed") if s.target == "inventory" else s
            for s in state.submissions
        )
        save_ots(sd, replace(state, submissions=subs))
        pre_calls = len(sp_fake._submissions)

        r = self._retry(runner, empty_sha256_root, staging, "obj:inv-retry-ok")
        assert r.exit_code == ExitCode.SUCCESS, r.output

        new_calls = sp_fake._submissions[pre_calls:]
        assert [tuple(c.digests) for c in new_calls] == [(inv_digest,)]
        state = load_ots(sd)
        assert state.phase is Phase.INVENTORY_PENDING
        (sub,) = [s for s in state.submissions if s.target == "inventory"]
        assert sub.status == "submitted"
        assert sub.attempts == 2

    def test_retry_without_a_failed_submission_sends_nothing(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)

        staging = tmp_path / "staging"
        sd, _da, _inv_digest = setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-retry-none", sp_fake
        )
        before = (sd / "ots.json").read_bytes()
        pre_calls = len(sp_fake._submissions)

        r = self._retry(runner, empty_sha256_root, staging, "obj:inv-retry-none")
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert "no failed submissions to retry" in r.output
        assert len(sp_fake._submissions) == pre_calls
        assert (sd / "ots.json").read_bytes() == before


@pytest.mark.bitcoin
class TestRegtestEndToEndInventoryUpgrade:
    def test_real_rpc_real_attestation_inventory(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        bitcoind_regtest,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from tests.helpers.ots import build_regtest_attestation
        from tests.helpers.regtest import bitcoin_config_toml, bury_to_confs

        cfg_body = SP_OK + bitcoin_config_toml(bitcoind_regtest, confirmations=3)
        monkeypatch.setenv("BITCOIN_RPC_USER", bitcoind_regtest.rpc_user)
        monkeypatch.setenv("BITCOIN_RPC_PASSWORD", bitcoind_regtest.rpc_password)
        write_config(tmp_path, monkeypatch, body=cfg_body)

        staging = tmp_path / "staging"
        sd, _da, inv_digest = setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:rt-inv", sp_fake
        )

        ots_bytes, attest_height, _ = build_regtest_attestation(
            bitcoind_regtest, bytes.fromhex(inv_digest)
        )
        seed_inv_completed_verify_job(sp_fake, sd, inv_digest, ots_bytes)
        bury_to_confs(bitcoind_regtest, attest_height, MIN_CONFS, "ocfltest")

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:rt-inv",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output

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
