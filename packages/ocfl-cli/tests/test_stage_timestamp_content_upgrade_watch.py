"""Tests for `ocfl stage upgrade --watch` on the content target.

Pins the `--watch` contract from `OTS workflow contract`:

- `stage upgrade OBJ --watch`: a ZMQ-driven loop that runs an upgrade pass per Bitcoin
  block; exits when phase advances to ``content-upgraded`` or on
  SIGINT/SIGTERM (exit 130).
- Loop sketch (per ``stage_timestamp_content_upgrade_watch.loop_sketch``):
    1. ``BitcoinZmqSubscriber`` (or fake ``BlockStream``) opened
       outside the lock.
    2. ``wait_for_block(timeout_ms=5000)`` — ``None`` ⇒ continue
       (re-check cancel flag).
    3. Acquire ``FileLock(staging/.lock, timeout=30)`` for write window
       only.
    4. Inside lock: fresh ``load_ots``, ``btc.getblockchaininfo()``,
       if ``height_jump > 1`` or tip-hash changed at stable height ⇒
       ``reverify_content_proofs``; run ``upgrade_content``.
    5. Release lock; if phase advanced to ``content-upgraded`` ⇒
       return 0.
- ``LockTimeoutError`` is logged at WARNING and absorbed (loop continues
  on next block).
- SIGINT/SIGTERM ⇒ caller-owned cancel flag; loop observes on next
  ``wait_for_block`` timeout (≤5s) or between passes; finishes
  in-flight pass; releases lock; exits 130. The crash-recovery
  invariant guarantees no torn checkpoint (``load_ots`` succeeds
  after).
- Two simultaneous ``--watch`` invocations on the same staging
  serialize safely via the staging flock.
- Caller (the watch loop), not the stream, owns the cancel flag —
  same loop shape used with the fake stream in tests.

Ports (``ocfl_cli.ports``): the stream, the post-block sleep and the
clients are injected through ``runner.obj`` (see ``conftest.py::runner``):
``block_stream_factory`` returns an ``InMemoryHashBlockStream`` (or an
inline stub), ``sleeper`` a ``RecordingSleeper``, and ``sp_client_factory``
/ ``btc_rpc_factory`` the SimpleProof and Bitcoin fakes.
"""

import dataclasses
import hashlib
import logging
import threading
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fakes import (
    FakeBitcoinRPC,
    FakeSimpleProof,
    RecordingSleeper,
)
from fakes.locks import held_flock
from msgspec.structs import replace
from runner_helpers import runner_obj
from typer.testing import CliRunner

from ocfl_cli.main import app
from ocfl_cli.model.ots_state import Submission, VerifyJob
from ocfl_cli.model.phase import Phase, TimestampTarget
from ocfl_cli.ports import BlockStream
from ocfl_cli.store.ots_state import load_ots, save_ots
from ocfl_cli.store.staging import StagingStore
from ocfl_lib import LocalFS, Spec, StorageRoot, get_layout
from tests.helpers.ots import build_attestation

# --------------------------------------------------------------------------- #
# Config + path helpers (copied from test_stage_timestamp_content_upgrade.py;
# the workflow rule is "smaller file > shared module before 3rd use").
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
    """Inject *stream* as the ``block_stream_factory`` port.

    Bypasses real ZMQ sockets. Accepts any
    ``BlockStream``-shaped object (``InMemoryHashBlockStream`` or
    inline test stub).
    """

    runner_obj(runner)["block_stream_factory"] = lambda *a, **kw: stream


@pytest.fixture(autouse=True)
def no_block_processing_delay(runner: CliRunner) -> None:

    runner_obj(runner)["sleeper"] = RecordingSleeper()


# --------------------------------------------------------------------------- #
# Storage-root + .ots/chain helpers (copied verbatim from upgrade tests).
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
RECHECKED = "Rechecking cached proofs after chain movement"
MIN_CONFS = 3


def _ots_for(digest_hex: str, *, block_height: int = DIGEST_BLOCK_HEIGHT) -> bytes:
    return build_attestation(bytes.fromhex(digest_hex), block_height=block_height)


class _ConfirmingStream:
    """Each wait mines one block (one more confirmation), then reports it.

    A watch runs one pass before the first block, so these tests start one
    confirmation short: only the block finishes the proof.
    """

    def __init__(self, rpc: FakeBitcoinRPC, *, blocks: int = 1) -> None:
        self.rpc = rpc
        self.blocks = blocks

    def wait_for_block(self, timeout_ms: int) -> bytes | None:
        if self.blocks == 0:
            raise KeyboardInterrupt("no more blocks")
        self.blocks -= 1
        self.rpc.extend(1)
        return bytes.fromhex(self.rpc.getblockchaininfo().best_block_hash)


def _grow_chain_to(rpc: FakeBitcoinRPC, height: int) -> None:
    needed = height - rpc.tip_height
    if needed > 0:
        rpc.extend(needed)


def _register_attested(
    rpc: FakeBitcoinRPC, digest_hex: str, height: int = DIGEST_BLOCK_HEIGHT
) -> None:
    rpc.register_merkle_root(height, bytes.fromhex(digest_hex)[::-1].hex())


def _new_ots_staging(
    runner: CliRunner,
    root: Path,
    staging: Path,
    object_id: str,
    *files: tuple[str, bytes],
) -> tuple[Path, dict[str, str], dict[str, str]]:
    r = runner.invoke(app, _stage_args(root, staging, "new", object_id, "--ots"))
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


# =========================================================================== #
# Layer-2: --watch loop with fake BlockStream — phase advance
# =========================================================================== #


class TestWatchLoopAdvancesOnBlock:
    def test_watch_exits_immediately_when_already_upgraded(
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

        runner_obj(runner)["block_stream_factory"] = lambda *a, **kw: pytest.fail(
            "already-upgraded watch subscribed to ZMQ",
        )

        staging = tmp_path / "staging"
        sd, _paths, digests = _new_ots_staging(
            runner, empty_sha256_root, staging, "obj:watch-done", ("a.txt", b"alpha")
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))
        ots_bytes = _ots_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        _register_attested(btc_fake.rpc, d)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:watch-done",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == 0, r.output
        assert load_ots(sd).phase is Phase.CONTENT_UPGRADED

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:watch-done",
                "--watch",
                "--expect",
                "content",
            ),
        )

        assert r.exit_code == 0, r.output
        assert "Watch:" in r.output
        assert "Object: obj:watch-done" in r.output
        assert "Phase: content-upgraded" in r.output
        assert "Next: ocfl stage timestamp obj:watch-done" in r.output
        assert "Waiting for Bitcoin blocks" not in r.output

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

        _inject_fake_zmq(runner, _ConfirmingStream(btc_fake.rpc))

        calls: list[str] = []

        runner_obj(runner)["sleeper"] = SimpleNamespace(
            sleep=lambda _seconds: calls.append("sleep")
        )

        staging = tmp_path / "staging"
        sd, _paths, digests = _new_ots_staging(
            runner, empty_sha256_root, staging, "obj:watch-delay", ("a.txt", b"alpha")
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes = _ots_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 2)
        _register_attested(btc_fake.rpc, d)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:watch-delay",
                "--watch",
                "--expect",
                "content",
            ),
        )

        assert r.exit_code == 0, r.output
        assert calls == ["sleep"]

    def test_watch_loop_advances_on_block(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One pre-pushed block ⇒ one upgrade pass that verifies + advances
        phase to content-upgraded; exits 0."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        _inject_fake_zmq(runner, _ConfirmingStream(btc_fake.rpc))

        staging = tmp_path / "staging"
        sd, _paths, digests = _new_ots_staging(
            runner, empty_sha256_root, staging, "obj:watch-ok", ("a.txt", b"alpha")
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes = _ots_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 2)
        _register_attested(btc_fake.rpc, d)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:watch-ok",
                "--watch",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == 0, r.output
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


# =========================================================================== #
# Layer-2: --watch reconcile on height jump > 1
# =========================================================================== #


class TestWatchHeightJumpTriggersReverify:
    def test_watch_height_jump_triggers_reverify(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two-digest staging: first digest verified before --watch starts.
        Between the two pushed block hashes, the chain jumps by >1
        (simulating dropped ZMQ notifications). The loop's per-wake
        ``getblockchaininfo`` notices and runs ``reverify_content_proofs``,
        which the progress output reports."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _paths, digests = _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:watch-jump",
            ("a.txt", b"alpha"),
            ("b.txt", b"bravo"),
        )
        da, db = digests["a.txt"], digests["b.txt"]
        _force_content_pending(sd, (da, db))

        ots_a = _ots_for(da)
        ots_b = _ots_for(db, block_height=DIGEST_BLOCK_HEIGHT + 1)
        _seed_completed_verify_job(sp_fake, sd, (da, db), {da: ots_a, db: ots_b})

        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        _register_attested(btc_fake.rpc, da)
        _register_attested(btc_fake.rpc, db, height=DIGEST_BLOCK_HEIGHT + 1)

        # Wake 1: tip at H. Wake 2: chain jumped by >1 between wakes ⇒
        # height_jump triggers reverify on second pass before the upgrade
        # pass runs. Inline stream lets us mutate the chain between waits.
        class _JumpStream:
            def __init__(self) -> None:
                self._n = 0

            def wait_for_block(self, timeout_ms: int) -> bytes | None:
                self._n += 1
                if self._n == 1:
                    return bytes.fromhex(
                        btc_fake.rpc.getblockchaininfo().best_block_hash
                    )
                if self._n == 2:
                    # Simulate a ZMQ-notification gap: chain advanced 5 blocks
                    # between hashblock messages.
                    btc_fake.rpc.extend(5)
                    return bytes.fromhex(
                        btc_fake.rpc.getblockchaininfo().best_block_hash
                    )
                # Loop should have exited on phase advance — but defensively
                # raise so a stuck loop fails fast.
                raise KeyboardInterrupt("loop did not exit after phase advance")

        _inject_fake_zmq(runner, _JumpStream())

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:watch-jump",
                "--watch",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == 0, r.output
        assert load_ots(sd).phase is Phase.CONTENT_UPGRADED
        # The chain moved between wakes, so the loop rechecked cached proofs.
        assert RECHECKED in r.output


# =========================================================================== #
# Layer-2: --watch reconcile on tip-hash change at stable height (reorg)
# =========================================================================== #


class TestWatchTipChangeAtStableHeightTriggersReverify:
    def test_watch_tip_change_at_stable_height_triggers_reverify(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Tip-hash differs at the same height between wakes ⇒ regtest
        reorg signal ⇒ ``reverify_content_proofs`` runs."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _paths, digests = _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:watch-reorg",
            ("a.txt", b"alpha"),
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes = _ots_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})
        # One confirmation short: wake 1 cannot advance the phase, so only
        # the tip change on wake 2 can make the loop recheck its proofs.
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 2)
        _register_attested(btc_fake.rpc, d)

        class _ReorgStream:
            def __init__(self) -> None:
                self._n = 0

            def wait_for_block(self, timeout_ms: int) -> bytes | None:
                self._n += 1
                if self._n == 1:
                    return bytes.fromhex(
                        btc_fake.rpc.getblockchaininfo().best_block_hash
                    )
                if self._n == 2:
                    # Same tip height; different tip hash (reorg).
                    btc_fake.rpc.reorg_at(btc_fake.rpc.tip_height)
                    _register_attested(btc_fake.rpc, d)
                    return bytes.fromhex(
                        btc_fake.rpc.getblockchaininfo().best_block_hash
                    )
                raise KeyboardInterrupt("loop did not exit after phase advance")

        _inject_fake_zmq(runner, _ReorgStream())

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:watch-reorg",
                "--watch",
                "--expect",
                "content",
            ),
        )
        # Still short of confirmations after the reorg: interrupted on
        # wake 3, after the tip change made the loop recheck cached proofs.
        assert r.exit_code == 130, r.output
        assert r.output.count(RECHECKED) == 1


# =========================================================================== #
# Layer-2: SIGINT mid-loop → exits 130 without torn checkpoint
# =========================================================================== #


class TestWatchSigintExits130WithoutTornCheckpoint:
    def test_watch_sigint_exits_130_without_torn_checkpoint(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Inject a stream whose first ``wait_for_block`` raises
        ``KeyboardInterrupt``. This is the deterministic substitute for
        sending a real SIGINT — the caller-owned cancel pattern means
        the loop catches the interrupt OR the loop body installs a
        signal handler that flips the cancel flag. Either way, the CLI
        must exit 130 and ``ots.json`` must parse cleanly afterwards."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _paths, digests = _new_ots_staging(
            runner, empty_sha256_root, staging, "obj:watch-sigint", ("a.txt", b"alpha")
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes = _ots_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 2)
        _register_attested(btc_fake.rpc, d)

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
                "obj:watch-sigint",
                "--watch",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == 130, r.output
        # No torn checkpoint: ots.json parses cleanly after SIGINT.
        load_ots(sd)


# =========================================================================== #
# Layer-2: LockTimeoutError absorbed (continue on next block)
# =========================================================================== #


class TestWatchLockTimeoutWarningAndContinue:
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
        """Pre-acquire the staging ``.lock``; push a block. The watch
        loop hits ``LockTimeoutError``, logs WARNING, continues. Then
        on the next wake we raise ``KeyboardInterrupt`` so the test
        terminates deterministically (exit 130). Assert the loop did
        NOT crash with ``LockTimeoutError`` and that a WARNING was
        logged with a message mentioning ``lock``.

        Speed: monkeypatch the watch loop's lock timeout down to 1s
        via the module-level ``LOCK_TIMEOUT_SECONDS`` constant the
        implementer is expected to introduce. If the implementer
        chooses an inline ``timeout=30`` literal, this test will pause
        for 30s before continuing — still correct, just slow."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        # Speed knob: monkeypatch the lock timeout if the constant exists.
        runner_obj(runner)["lock_timeout"] = 1

        staging = tmp_path / "staging"
        sd, _paths, digests = _new_ots_staging(
            runner, empty_sha256_root, staging, "obj:watch-lock", ("a.txt", b"alpha")
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes = _ots_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        _register_attested(btc_fake.rpc, d)

        # Hold the staging .lock as another process would.
        sibling = ExitStack()
        sibling.enter_context(held_flock(sd / ".lock"))

        class _ContendedStream:
            """First wake delivers a block (which will hit LockTimeoutError);
            second wake raises KeyboardInterrupt to terminate the test."""

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
                        "obj:watch-lock",
                        "--watch",
                        "--expect",
                        "content",
                    ),
                )
        finally:
            sibling.close()

        # Loop terminated via the simulated SIGINT, NOT via an unhandled
        # LockTimeoutError. Exit 130 (graceful cancel).
        assert r.exit_code == 130, r.output
        # No torn checkpoint after lock contention + cancel.
        load_ots(sd)
        # WARNING was logged with message mentioning the lock.
        warn_msgs = [
            r.getMessage().lower()
            for r in caplog.records
            if r.levelno == logging.WARNING
        ]
        assert any("lock" in m for m in warn_msgs), warn_msgs


# =========================================================================== #
# Layer-2: two --watchers on same staging serialize safely
# =========================================================================== #


class TestTwoWatchersSerializeSafely:
    def test_two_watchers_serialize_safely(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        runtime,
    ) -> None:
        """Spawn two CliRunner invocations on separate threads, each
        driven by an independent fake stream that delivers one block.
        Both must exit 0; final state has no torn writes; phase
        advanced to content-upgraded; the proof verifies."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _paths, digests = _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:watch-two",
            ("a.txt", b"alpha"),
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes = _ots_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        _register_attested(btc_fake.rpc, d)

        from ocfl_cli.config import load_config
        from ocfl_cli.model.phase import TimestampTarget
        from ocfl_cli.workflow.timestamp import TimestampRun
        from ocfl_cli.workflow.watch import watch
        from ocfl_lib import LocalFS, StorageRoot

        # Two independent streams; each delivers one block then raises
        # KeyboardInterrupt so the loser exits cleanly after the winner
        # advances the phase.
        def _make_stream() -> object:
            class _S:
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

        # Round-robin: each thread gets a fresh stream so the two
        # invocations don't share state.
        streams: list[object] = [_make_stream(), _make_stream()]
        idx = {"i": 0}
        idx_lock = threading.Lock()

        def _next_stream(*a: object, **kw: object) -> object:
            with idx_lock:
                i = idx["i"]
                idx["i"] = i + 1
            return streams[i]

        thread_runtime = dataclasses.replace(runtime, block_streams=_next_stream)

        # Call watch directly per thread. CliRunner patches
        # process-global sys.stdout/err which is not thread-safe; the
        # contract under test (flock serialization) lives entirely in
        # watch. Per-thread state (cfg/store/sr) is built locally;
        # only the staging dir + its flock are shared.
        cfg = load_config()
        root_path = str(empty_sha256_root.resolve())
        # Both threads start at the same moment so the race against the
        # flock is real, not whichever-thread-started-first.
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
        # Surface any thread exceptions in the main thread.
        assert not errors, errors
        # Winner exits 0 on phase advance; loser observes either
        # phase-already-advanced (0) or KeyboardInterrupt (130).
        for tag, code in results.items():
            assert code in (0, 130), f"{tag}: exit={code}"

        # Final state: phase advanced; proof verified; no torn writes.
        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_UPGRADED
        assert state.proofs[d].verified is True


# =========================================================================== #
# Layer-3: regtest real ZMQ hashblock delivery
# =========================================================================== #


@pytest.mark.bitcoin
class TestRegtestRealZmqHashblockDelivery:
    def test_real_zmq_hashblock_delivery_drives_watch_loop(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        bitcoind_regtest,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Real bitcoind ZMQ ``hashblock`` notifications drive ``--watch``.

        Anchors ``digest`` at height ``H`` with only ``MIN_CONFS - 1``
        confirmations so the first upgrade pass does not advance the
        phase. The watch loop then waits on real ZMQ; mining one more
        block delivers the notification, the next pass sees enough
        confirmations, the phase advances to ``content-upgraded``, and
        the command exits 0.
        """
        from tests.helpers.ots import build_regtest_attestation
        from tests.helpers.regtest import bitcoin_config_toml

        cfg_body = _SP_OK + bitcoin_config_toml(bitcoind_regtest, confirmations=3)
        monkeypatch.setenv("BITCOIN_RPC_USER", bitcoind_regtest.rpc_user)
        monkeypatch.setenv("BITCOIN_RPC_PASSWORD", bitcoind_regtest.rpc_password)
        _write_config(tmp_path, monkeypatch, body=cfg_body)
        _inject_fake_sp(runner, sp_fake)
        # Production btc_rpc_factory + block_stream_factory paths used.

        staging = tmp_path / "staging"
        sd, _paths, digests = _new_ots_staging(
            runner, empty_sha256_root, staging, "obj:rt-watch", ("a.txt", b"alpha")
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes, attest_height, _ = build_regtest_attestation(
            bitcoind_regtest, bytes.fromhex(d)
        )
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})

        # Bring the chain to MIN_CONFS - 1 confirmations: not enough to
        # verify yet. The watch loop's first pass will record
        # verified=False; the next mined block (delivered via real ZMQ)
        # tips us to MIN_CONFS and lets the pass advance the phase.
        addr = bitcoind_regtest.wallet_rpc("ocfltest", "getnewaddress")
        info = bitcoind_regtest.rpc("getblockchaininfo")
        confs = int(info["blocks"]) - attest_height + 1
        if confs < MIN_CONFS - 1:
            bitcoind_regtest.rpc("generatetoaddress", [MIN_CONFS - 1 - confs, addr])

        # Run --upgrade --watch in a thread; mine the unblocking block
        # from the main thread once the loop is up.
        result: dict[str, Any] = {}

        # Fresh CliRunner per thread (process-global stdout/stderr issues
        # with the fixture runner under threads); pass `runner.obj` so the
        # injected sp_client_factory still flows through.
        thread_obj = dict(runner_obj(runner))

        def _run() -> None:
            r = CliRunner().invoke(
                app,
                _stage_args(
                    empty_sha256_root,
                    staging,
                    "upgrade",
                    "obj:rt-watch",
                    "--watch",
                    "--expect",
                    "content",
                ),
                obj=thread_obj,
            )
            result["exit"] = r.exit_code
            result["output"] = r.output

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        # Give the SUB socket time to subscribe before mining the
        # notification block; without this the subscriber may miss it.
        import time as _time

        _time.sleep(1.0)
        bitcoind_regtest.rpc("generatetoaddress", [1, addr])

        thread.join(timeout=30.0)
        assert not thread.is_alive(), "watch loop did not exit after block notify"
        assert result.get("exit") == 0, result
        assert load_ots(sd).phase is Phase.CONTENT_UPGRADED


# =========================================================================== #
# Layer-2: one pass before the first block (roadmap §7.4, issue 2)
# =========================================================================== #


class TestWatchFirstPass:
    def test_final_proofs_do_not_wait_for_a_block(
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

        class _NoBlocks:
            waits = 0

            def wait_for_block(self, timeout_ms: int) -> bytes | None:
                self.waits += 1
                raise KeyboardInterrupt("waited for a block")

        stream = _NoBlocks()
        _inject_fake_zmq(runner, stream)

        staging = tmp_path / "staging"
        sd, _paths, digests = _new_ots_staging(
            runner, empty_sha256_root, staging, "obj:first-pass", ("a.txt", b"alpha")
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: _ots_for(d)})
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        _register_attested(btc_fake.rpc, d)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root, staging, "upgrade", "obj:first-pass", "--watch"
            ),
        )

        assert r.exit_code == 0, r.output
        assert stream.waits == 0
        assert load_ots(sd).phase is Phase.CONTENT_UPGRADED
        assert "Phase: content-pending -> content-upgraded" in r.output
