import dataclasses
import json
import logging
import threading
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pytest
from fakes import (
    FakeBitcoinRPC,
    FakeBlockStream,
    FakeSimpleProof,
    RecordingSleeper,
)
from fakes.locks import held_flock
from msgspec.structs import replace
from ots_helpers import (
    DIGEST_BLOCK_HEIGHT,
    MIN_CONFS,
    SP_OK,
    bury,
    final_proof_for,
    inject_fake_btc,
    inject_fake_sp,
    new_ots_staging,
    register_attested,
    write_config,
)
from runner_helpers import ObjRunner, stage_args
from typer.testing import CliRunner

from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app
from ocfl_cli.model.ots_state import Submission, VerifyJob
from ocfl_cli.model.phase import Phase, TimestampTarget
from ocfl_cli.ports import BlockStream
from ocfl_cli.store.ots_state import load_ots, save_ots
from ocfl_cli.store.staging import StagingStore
from ocfl_lib import LocalFS, StorageRoot
from ocfl_lib.bitcoin import BlockHeader


def _inject_fake_zmq(runner: ObjRunner, stream: BlockStream) -> None:

    runner.ports.block_stream_factory = lambda *a, **kw: stream


@pytest.fixture(autouse=True)
def no_block_processing_delay(runner: ObjRunner) -> None:

    runner.ports.sleeper = RecordingSleeper()


RECHECKED = "Rechecking cached proofs after chain movement"


class _MinesOneBlockPerWait(FakeBlockStream):
    def __init__(self, rpc: FakeBitcoinRPC, *, blocks: int = 1) -> None:
        self.rpc = rpc
        self.blocks = blocks

    def wait_for_block(self, timeout_ms: int) -> bytes | None:
        if self.blocks == 0:
            raise KeyboardInterrupt("no more blocks")
        self.blocks -= 1
        self.rpc.extend(1)
        return bytes.fromhex(self.rpc.getblockchaininfo().best_block_hash)


def _force_content_pending(sd: Path, digests: tuple[str, ...]) -> None:
    state = load_ots(sd)
    state = replace(
        state,
        phase=Phase.CONTENT_PENDING,
        pending_digests=digests,
        submissions=(
            Submission(
                submitted_at="2026-04-23T16:30:00+00:00",
                digests=digests,
                status="submitted",
                target=TimestampTarget.CONTENT,
                attempts=1,
            ),
        ),
    )
    save_ots(sd, state)


def _seed_completed_verify_job(
    sp_fake: FakeSimpleProof,
    sd: Path,
    digests: tuple[str, ...],
    ots_results: dict[str, bytes],
) -> str:
    job_id = sp_fake.verify_batch(list(digests)).job_id
    sp_fake.advance_to_completed(job_id, ots_results)
    state = load_ots(sd)
    state = replace(
        state,
        verify_jobs=(
            *state.verify_jobs,
            VerifyJob(job_id=job_id, digests=digests, status="IN_PROGRESS"),
        ),
    )
    save_ots(sd, state)
    return job_id


class TestWatchLoopAdvancesOnBlock:
    def test_watch_exits_immediately_when_already_upgraded(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)
        inject_fake_btc(runner, btc_fake.rpc)

        runner.ports.block_stream_factory = lambda *a, **kw: pytest.fail(
            "already-upgraded watch subscribed to ZMQ",
        )

        staging = tmp_path / "staging"
        sd, digests = new_ots_staging(
            runner, empty_sha256_root, staging, "obj:watch-done", ("a.txt", b"alpha")
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))
        ots_bytes = final_proof_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})
        bury(btc_fake.rpc)
        register_attested(btc_fake.rpc, d)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:watch-done",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert load_ots(sd).phase is Phase.CONTENT_UPGRADED

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:watch-done",
                "--watch",
                "--expect",
                "content",
            ),
        )

        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert "Watch:" in r.output
        assert "Object: obj:watch-done" in r.output
        assert "Phase: content-upgraded" in r.output
        assert "Next: ocfl stage timestamp obj:watch-done" in r.output
        assert "Waiting for Bitcoin blocks" not in r.output

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
        inject_fake_sp(runner, sp_fake)
        inject_fake_btc(runner, btc_fake.rpc)

        _inject_fake_zmq(runner, _MinesOneBlockPerWait(btc_fake.rpc))

        sleeper = RecordingSleeper()
        runner.ports.sleeper = sleeper

        staging = tmp_path / "staging"
        sd, digests = new_ots_staging(
            runner, empty_sha256_root, staging, "obj:watch-delay", ("a.txt", b"alpha")
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes = final_proof_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})
        bury(btc_fake.rpc, MIN_CONFS - 1)
        register_attested(btc_fake.rpc, d)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:watch-delay",
                "--watch",
                "--expect",
                "content",
            ),
        )

        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert len(sleeper.calls) == 1

    def test_watch_loop_advances_on_block(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)
        inject_fake_btc(runner, btc_fake.rpc)

        _inject_fake_zmq(runner, _MinesOneBlockPerWait(btc_fake.rpc))

        staging = tmp_path / "staging"
        sd, digests = new_ots_staging(
            runner, empty_sha256_root, staging, "obj:watch-ok", ("a.txt", b"alpha")
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes = final_proof_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})
        bury(btc_fake.rpc, MIN_CONFS - 1)
        register_attested(btc_fake.rpc, d)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:watch-ok",
                "--watch",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert "Watch:" in r.output
        assert "Object: obj:watch-ok" in r.output
        assert "Waiting for Bitcoin blocks" in r.output
        assert "Block:" in r.output
        assert "Height:" in r.output
        assert "Checking SimpleProof jobs" in r.output
        assert "Phase: content-pending" in r.output
        assert "Workflow:" in r.output
        assert "Phase: content-pending -> content-upgraded" in r.output
        assert "Next: ocfl stage timestamp obj:watch-ok" in r.output
        assert load_ots(sd).phase is Phase.CONTENT_UPGRADED

    def test_json_watch_ends_in_summary_and_noop_verb_exits_0(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)
        inject_fake_btc(runner, btc_fake.rpc)
        _inject_fake_zmq(runner, _MinesOneBlockPerWait(btc_fake.rpc))

        staging = tmp_path / "staging"
        sd, digests = new_ots_staging(
            runner, empty_sha256_root, staging, "obj:watch-json", ("a.txt", b"alpha")
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: final_proof_for(d)})
        bury(btc_fake.rpc, MIN_CONFS - 1)
        register_attested(btc_fake.rpc, d)

        upgrade = [
            "--json",
            *stage_args(empty_sha256_root, staging, "upgrade", "obj:watch-json"),
        ]
        r = runner.invoke(app, [*upgrade, "--watch"])

        assert r.exit_code == ExitCode.SUCCESS, r.output
        events = [json.loads(line) for line in r.stdout.splitlines()]
        assert len(events) > 1
        assert [e["event"] for e in events].count("summary") == 1
        assert events[-1] == {
            "event": "summary",
            "object_id": "obj:watch-json",
            "verb": "upgrade",
            "target": "content",
            "phase_before": "content-pending",
            "phase_after": "content-upgraded",
            "done": True,
            "next": "ocfl stage timestamp obj:watch-json",
        }

        r = runner.invoke(app, upgrade)

        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert isinstance(json.loads(r.stdout), dict)


class TestWatchHeightJumpTriggersReverify:
    def test_watch_reverifies_when_dropped_notifications_jump_height(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)
        inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, digests = new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:watch-jump",
            ("a.txt", b"alpha"),
            ("b.txt", b"bravo"),
        )
        da, db = digests["a.txt"], digests["b.txt"]
        _force_content_pending(sd, (da, db))

        ots_a = final_proof_for(da)
        ots_b = final_proof_for(db, block_height=DIGEST_BLOCK_HEIGHT + 1)
        _seed_completed_verify_job(sp_fake, sd, (da, db), {da: ots_a, db: ots_b})

        bury(btc_fake.rpc)
        register_attested(btc_fake.rpc, da)
        register_attested(btc_fake.rpc, db, height=DIGEST_BLOCK_HEIGHT + 1)

        dropped_notifications = 5

        class _JumpStream(FakeBlockStream):
            def __init__(self) -> None:
                self._n = 0

            def wait_for_block(self, timeout_ms: int) -> bytes | None:
                self._n += 1
                if self._n == 1:
                    return bytes.fromhex(
                        btc_fake.rpc.getblockchaininfo().best_block_hash
                    )
                if self._n == 2:
                    btc_fake.rpc.extend(dropped_notifications)
                    return bytes.fromhex(
                        btc_fake.rpc.getblockchaininfo().best_block_hash
                    )
                raise KeyboardInterrupt("loop did not exit after phase advance")

        _inject_fake_zmq(runner, _JumpStream())

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:watch-jump",
                "--watch",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert load_ots(sd).phase is Phase.CONTENT_UPGRADED
        assert RECHECKED in r.output


class TestWatchTipChangeAtStableHeightTriggersReverify:
    def test_watch_tip_change_at_stable_height_triggers_reverify(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)
        inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, digests = new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:watch-reorg",
            ("a.txt", b"alpha"),
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes = final_proof_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})
        bury(btc_fake.rpc, MIN_CONFS - 1)
        register_attested(btc_fake.rpc, d)

        class _ReorgStream(FakeBlockStream):
            def __init__(self) -> None:
                self._n = 0

            def wait_for_block(self, timeout_ms: int) -> bytes | None:
                self._n += 1
                if self._n == 1:
                    return bytes.fromhex(
                        btc_fake.rpc.getblockchaininfo().best_block_hash
                    )
                if self._n == 2:
                    btc_fake.rpc.reorg_at(btc_fake.rpc.tip_height)
                    register_attested(btc_fake.rpc, d)
                    return bytes.fromhex(
                        btc_fake.rpc.getblockchaininfo().best_block_hash
                    )
                raise KeyboardInterrupt("loop did not exit after phase advance")

        _inject_fake_zmq(runner, _ReorgStream())

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:watch-reorg",
                "--watch",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.INTERRUPTED, r.output
        assert r.output.count(RECHECKED) == 1


class TestWatchSigintExits130WithoutTornCheckpoint:
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
        inject_fake_sp(runner, sp_fake)
        inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, digests = new_ots_staging(
            runner, empty_sha256_root, staging, "obj:watch-sigint", ("a.txt", b"alpha")
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes = final_proof_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})
        bury(btc_fake.rpc, MIN_CONFS - 1)
        register_attested(btc_fake.rpc, d)

        class _SigintStream(FakeBlockStream):
            def wait_for_block(self, timeout_ms: int) -> bytes | None:
                raise KeyboardInterrupt("simulated SIGINT before first block")

        _inject_fake_zmq(runner, _SigintStream())

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:watch-sigint",
                "--watch",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.INTERRUPTED, r.output
        load_ots(sd)


class TestWatchLockTimeoutWarningAndContinue:
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
        inject_fake_sp(runner, sp_fake)
        inject_fake_btc(runner, btc_fake.rpc)

        runner.ports.lock_timeout = 1

        staging = tmp_path / "staging"
        sd, digests = new_ots_staging(
            runner, empty_sha256_root, staging, "obj:watch-lock", ("a.txt", b"alpha")
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes = final_proof_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})
        bury(btc_fake.rpc)
        register_attested(btc_fake.rpc, d)

        sibling = ExitStack()
        sibling.enter_context(held_flock(sd / ".lock"))

        class _ContendedStream(FakeBlockStream):
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
                        "obj:watch-lock",
                        "--watch",
                        "--expect",
                        "content",
                    ),
                )
        finally:
            sibling.close()

        assert r.exit_code == ExitCode.INTERRUPTED, r.output
        load_ots(sd)
        warn_msgs = [
            r.getMessage().lower()
            for r in caplog.records
            if r.levelno == logging.WARNING
        ]
        assert any("lock" in m for m in warn_msgs), warn_msgs


class TestTwoWatchersSerializeSafely:
    def test_two_watchers_serialize_safely(
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
        inject_fake_sp(runner, sp_fake)
        inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, digests = new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:watch-two",
            ("a.txt", b"alpha"),
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes = final_proof_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})
        bury(btc_fake.rpc)
        register_attested(btc_fake.rpc, d)

        from ocfl_cli.config import load_config
        from ocfl_cli.model.phase import TimestampTarget
        from ocfl_cli.workflow.timestamp import TimestampRun
        from ocfl_cli.workflow.watch import watch

        def _make_stream() -> object:
            class _S(FakeBlockStream):
                def __init__(self) -> None:
                    self._n = 0

                def wait_for_block(self, timeout_ms: int) -> bytes | None:
                    self._n += 1
                    if self._n == 1:
                        return bytes.fromhex(
                            btc_fake.rpc.getblockchaininfo().best_block_hash
                        )
                    raise KeyboardInterrupt("end")

            return _S()

        streams: list[object] = [_make_stream(), _make_stream()]
        idx = {"i": 0}
        idx_lock = threading.Lock()

        def _next_stream(*a: object, **kw: object) -> object:
            with idx_lock:
                i = idx["i"]
                idx["i"] = i + 1
            return streams[i]

        thread_runtime = dataclasses.replace(runtime, block_streams=_next_stream)

        # Call watch directly per thread: CliRunner patches
        # process-global sys.stdout/err, which is not thread-safe.
        cfg = load_config()
        root_path = str(empty_sha256_root.resolve())
        barrier = threading.Barrier(2)
        results: dict[str, int] = {}
        errors: dict[str, BaseException] = {}

        def _run(tag: str) -> None:
            try:
                store = StagingStore(staging)
                sr = StorageRoot.open(LocalFS(empty_sha256_root))
                barrier.wait(timeout=10)
                run = TimestampRun(
                    cfg=cfg,
                    store=store,
                    sr=sr,
                    root_path=root_path,
                    object_id="obj:watch-two",
                    staging_dir=sd,
                    sp_factory=lambda _cfg: sp_fake,
                    btc_factory=lambda _cfg: btc_fake.rpc,
                    runtime=thread_runtime,
                )
                results[tag] = watch(run, expect=TimestampTarget.CONTENT)
            except BaseException as e:
                errors[tag] = e

        t1 = threading.Thread(target=_run, args=("a",))
        t2 = threading.Thread(target=_run, args=("b",))
        t1.start()
        t2.start()
        t1.join(timeout=60)
        t2.join(timeout=60)

        assert not t1.is_alive()
        assert not t2.is_alive()
        assert not errors, errors
        for tag, code in results.items():
            assert code in (ExitCode.SUCCESS, ExitCode.INTERRUPTED), (
                f"{tag}: exit={code}"
            )

        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_UPGRADED
        assert state.proofs[d].verified is True


@pytest.mark.bitcoin
class TestRegtestRealZmqHashblockDelivery:
    def test_real_zmq_hashblock_delivery_drives_watch_loop(
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
        inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, digests = new_ots_staging(
            runner, empty_sha256_root, staging, "obj:rt-watch", ("a.txt", b"alpha")
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes, attest_height, _ = build_regtest_attestation(
            bitcoind_regtest, bytes.fromhex(d)
        )
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})

        bury_to_confs(bitcoind_regtest, attest_height, MIN_CONFS - 1, "ocfltest")

        result: dict[str, Any] = {}

        # Fresh CliRunner per thread: the fixture runner's stdout/stderr
        # swap is process-global.
        def _run() -> None:
            r = CliRunner().invoke(
                app,
                stage_args(
                    empty_sha256_root,
                    staging,
                    "upgrade",
                    "obj:rt-watch",
                    "--watch",
                    "--expect",
                    "content",
                ),
                obj=runner.ports,
            )
            result["exit"] = r.exit_code
            result["output"] = r.output

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        # Give the SUB socket time to subscribe before mining the
        # notification block; without this the subscriber may miss it.
        import time as _time

        _time.sleep(1.0)
        addr = bitcoind_regtest.wallet_rpc("ocfltest", "getnewaddress")
        bitcoind_regtest.rpc("generatetoaddress", [1, addr])

        thread.join(timeout=30.0)
        assert not thread.is_alive(), "watch loop did not exit after block notify"
        assert result.get("exit") == ExitCode.SUCCESS, result
        assert load_ots(sd).phase is Phase.CONTENT_UPGRADED


class TestWatchFirstPass:
    def test_final_proofs_do_not_wait_for_a_block(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)
        inject_fake_btc(runner, btc_fake.rpc)

        class _NoBlocks(FakeBlockStream):
            waits = 0

            def wait_for_block(self, timeout_ms: int) -> bytes | None:
                self.waits += 1
                raise KeyboardInterrupt("waited for a block")

        stream = _NoBlocks()
        _inject_fake_zmq(runner, stream)

        staging = tmp_path / "staging"
        sd, digests = new_ots_staging(
            runner, empty_sha256_root, staging, "obj:first-pass", ("a.txt", b"alpha")
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: final_proof_for(d)})
        bury(btc_fake.rpc)
        register_attested(btc_fake.rpc, d)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root, staging, "upgrade", "obj:first-pass", "--watch"
            ),
        )

        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert stream.waits == 0
        assert load_ots(sd).phase is Phase.CONTENT_UPGRADED
        assert "Phase: content-pending -> content-upgraded" in r.output

    def test_block_found_during_first_pass_is_not_missed(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _Subscription(FakeBlockStream):
            def __init__(self) -> None:
                self.feed: list[bytes] = []

            def wait_for_block(self, timeout_ms: int) -> bytes | None:
                if not self.feed:
                    raise KeyboardInterrupt("no block delivered")
                return self.feed.pop(0)

        subscriptions: list[_Subscription] = []

        def subscribe(*a: object, **kw: object) -> _Subscription:
            subscriptions.append(_Subscription())
            return subscriptions[-1]

        class _MinesAfterFirstHeader(FakeBitcoinRPC):
            mined = False

            def getblockheader(self, block_hash: str) -> BlockHeader:
                header = super().getblockheader(block_hash)
                if not self.mined:
                    self.mined = True
                    self.extend(1)
                    tip = bytes.fromhex(self.getblockchaininfo().best_block_hash)
                    for s in subscriptions:
                        s.feed.append(tip)
                return header

        rpc = _MinesAfterFirstHeader()
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)
        inject_fake_btc(runner, rpc)
        runner.ports.block_stream_factory = subscribe

        staging = tmp_path / "staging"
        sd, digests = new_ots_staging(
            runner, empty_sha256_root, staging, "obj:mid-pass", ("a.txt", b"alpha")
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: final_proof_for(d)})
        bury(rpc, MIN_CONFS - 1)
        register_attested(rpc, d)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root, staging, "upgrade", "obj:mid-pass", "--watch"
            ),
        )

        assert rpc.mined
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert load_ots(sd).phase is Phase.CONTENT_UPGRADED
