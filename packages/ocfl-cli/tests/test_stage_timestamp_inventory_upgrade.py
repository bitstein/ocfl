"""Tests for `ocfl stage timestamp inventory --upgrade / --watch / --retry`.

Pins step 15 of `OTS workflow contract` — the inventory-side mirror of
the content `--upgrade`/`--watch`/`--retry` shape:

- ``--upgrade``: single-digest (the inventory digest) verify pass with
  the same crash-recovery invariant as content ``--upgrade``. Step (1)
  atomic cache write at ``ots-cache/<inv_digest>.ots``; step (2) ingest
  is VACUOUS for inventory (the extension-file entry is queued at
  commit time, not now); step (3) atomic ``ots.json`` update sets
  ``inventory_proof.proof = VerifiedProof(verified=True, block_height,
  confirmations_at_verify, verified_at, ots_path=ots-cache/<d>.ots)``
  and advances phase to ``inventory-upgraded``.
- ``--watch``: ZMQ-driven loop that runs an upgrade pass per Bitcoin
  block; exits 0 when phase advances to ``inventory-upgraded`` or 130
  on SIGINT/SIGTERM.
- ``--retry``: re-POST the inventory digest at phase
  ``INVENTORY_PENDING``; ``PhaseError`` (exit 3) at any other phase.
  Mutually exclusive with ``--upgrade`` / ``--watch``.

These tests are RED until the three flags + corresponding code paths
land. Expected diagnostics include ``no such option: '--upgrade'``,
``KeyError`` on ``inventory_proof.proof``, or ``NotImplementedError``.
"""

import hashlib
import json
import logging
import os
from pathlib import Path

import filelock
import pytest
from fakes import FakeBitcoinRPC, FakeSimpleProof
from msgspec.structs import replace
from runner_helpers import runner_obj
from typer.testing import CliRunner

from ocfl_cli.main import app
from ocfl_cli.ots.bitcoin import HashBlockStream
from ocfl_cli.stage.fsm import Phase
from ocfl_cli.stage.state import (
    VerifiedProof,
    VerifyJob,
    load_ots,
    ots_cache_path,
    save_ots,
)
from ocfl_cli.stage.store import StagingStore
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
access_token = "tok"
api_key = "key"
"""

_BTC_OK_COOKIE = """\
[bitcoin]
rpc_url = "http://127.0.0.1:8332"
zmq_endpoint = "tcp://127.0.0.1:28332"
rpc_user = "u"
rpc_password = "p"
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


def _inject_fake_zmq(monkeypatch: pytest.MonkeyPatch, stream: HashBlockStream) -> None:
    from ocfl_cli.stage import watch as stage_watch

    monkeypatch.setattr(
        stage_watch, "_get_zmq_subscriber", lambda *a, **kw: stream, raising=False
    )


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
    ots_src.write_bytes(b"\x00fake-ots-bytes-opaque-to-this-command")
    r = runner.invoke(app, _stage_args(root, staging, "add", object_id, str(ots_src)))
    assert r.exit_code == 0, r.output

    state = load_ots(sd)
    state = replace(
        state,
        phase=Phase.CONTENT_UPGRADED,
        pending_digests=(da,),
        content_proofs={
            da: VerifiedProof(
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
        app, _stage_args(root, staging, "timestamp", "inventory", object_id)
    )
    assert r.exit_code == 0, r.output

    state = load_ots(sd)
    assert state.phase is Phase.INVENTORY_PENDING
    assert state.inventory_proof is not None
    return sd, da, state.inventory_proof.digest


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
      - ``state.inventory_proof.job_id`` (single-digest convenience)

    The upgrade pass should detect the existing job, poll, see
    COMPLETED, then download + verify.
    """
    job_id = sp_fake.verify_batch([inv_digest])
    sp_fake.advance_to_completed(job_id, {inv_digest: ots_bytes})

    state = load_ots(sd)
    assert state.inventory_proof is not None
    new_inv = replace(state.inventory_proof, job_id=job_id)
    state = replace(
        state,
        inventory_proof=new_inv,
        verify_jobs=(
            *state.verify_jobs,
            VerifyJob(job_id=job_id, digests=(inv_digest,), status="IN_PROGRESS"),
        ),
    )
    save_ots(sd, state)
    return job_id


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
                "timestamp",
                "inventory",
                "obj:inv-up",
                "--upgrade",
            ),
        )
        assert r.exit_code == 0, r.output

        state = load_ots(sd)
        assert state.phase is Phase.INVENTORY_UPGRADED
        assert state.inventory_proof is not None
        proof = state.inventory_proof.proof
        assert proof.verified is True
        assert proof.block_height == DIGEST_BLOCK_HEIGHT
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
                    "timestamp",
                    "inventory",
                    "obj:inv-crash",
                    "--upgrade",
                ),
            )
            assert r.exit_code != 0, r.output
            # Crash-recovery invariant: ots.json is never torn (always parses).
            post_crash = load_ots(sd)
            # If verified=True is recorded, cache bytes must already exist
            # (the implication direction of the three-step invariant).
            if (
                post_crash.inventory_proof is not None
                and post_crash.inventory_proof.proof.verified is True
            ):
                assert ots_cache_path(sd, inv_digest).is_file()

            _uninstall_crash_fault(monkeypatch)

        # Round 2 (or sole pass): converges to inventory-upgraded.
        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "inventory",
                "obj:inv-crash",
                "--upgrade",
            ),
        )
        assert r.exit_code == 0, r.output
        state = load_ots(sd)
        assert state.phase is Phase.INVENTORY_UPGRADED
        assert state.inventory_proof is not None
        assert state.inventory_proof.proof.verified is True
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
    """Wrap module-level seams to raise at the specified inventory boundary.

    ``_atomic_write_bytes`` from ``ocfl_cli.stage.store`` underlies BOTH
    the ots-cache write (step 1) and the ots.json save (step 3). Fault
    site is selected by inspecting the target path. The wire format
    flattens ``inventory_proof.proof`` into the top-level
    ``inventory_proof`` object, so the JSON inspection looks at
    ``inventory_proof.verified``.
    """
    from ocfl_cli.stage import store as staging_mod

    real_atomic = staging_mod._atomic_write_bytes

    cache_path = ots_cache_path(sd, inv_digest)
    ots_json_path = sd / "ots.json"

    if fault_at == "before_cache":

        def _atomic(path: Path, data: bytes) -> None:
            if path == cache_path:
                raise OSError("injected: inventory cache write failed")
            real_atomic(path, data)

        _patch_attr(staging_mod, "_atomic_write_bytes", _atomic)

    elif fault_at == "during_ots_save":
        # Fault inside the atomic helper between temp-write and replace
        # ONLY when the about-to-be-written state carries
        # inventory_proof.verified=True (so we don't crash on earlier
        # benign saves like the verify-job append).
        def _atomic(path: Path, data: bytes) -> None:
            if path == ots_json_path:
                try:
                    parsed = json.loads(data)
                    ip = parsed.get("inventory_proof") or {}
                    if (
                        isinstance(ip, dict)
                        and ip.get("verified") is True
                        and ip.get("digest") == inv_digest
                    ):
                        # Write tmp, then crash before replace.
                        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
                        with open(tmp, "wb") as f:
                            f.write(data)
                            f.flush()
                            os.fsync(f.fileno())
                        raise OSError("injected: crash mid-save (inventory)")
                except json.JSONDecodeError, AttributeError:
                    pass
            real_atomic(path, data)

        _patch_attr(staging_mod, "_atomic_write_bytes", _atomic)

    else:
        raise AssertionError(f"unknown fault_at: {fault_at!r}")


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
                "timestamp",
                "inventory",
                "obj:inv-adv",
                "--upgrade",
            ),
        )
        assert r.exit_code == 0, r.output
        state = load_ots(sd)
        assert state.phase is Phase.INVENTORY_UPGRADED
        assert state.inventory_proof is not None
        assert state.inventory_proof.proof.verified is True

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
                "timestamp",
                "inventory",
                "obj:inv-pend",
                "--upgrade",
            ),
        )
        assert r.exit_code == 0, r.output
        state = load_ots(sd)
        assert state.phase is Phase.INVENTORY_PENDING
        assert state.inventory_proof is not None
        assert state.inventory_proof.proof.verified is False
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
                "timestamp",
                "inventory",
                "obj:inv-shallow",
                "--upgrade",
            ),
        )
        assert r.exit_code == 0, r.output
        state = load_ots(sd)
        assert state.phase is Phase.INVENTORY_PENDING
        assert state.inventory_proof is not None
        assert state.inventory_proof.proof.verified is False


# =========================================================================== #
# Layer-2: --watch loop for inventory
# =========================================================================== #


class TestInventoryWatchLoop:
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

        _inject_fake_zmq(monkeypatch, _OneBlockStream())

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "inventory",
                "obj:inv-watch-ok",
                "--upgrade",
                "--watch",
            ),
        )
        assert r.exit_code == 0, r.output
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

        _inject_fake_zmq(monkeypatch, _SigintStream())

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "inventory",
                "obj:inv-watch-sigint",
                "--upgrade",
                "--watch",
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

        from ocfl_cli.stage import service as stage_cmd

        # Speed knob: shrink the watch loop's lock timeout if the constant
        # exists (it does — see test_stage_timestamp_content_upgrade_watch).
        monkeypatch.setattr(stage_cmd, "_LOCK_TIMEOUT_SECONDS", 1, raising=False)

        staging = tmp_path / "staging"
        sd, _da, inv_digest = _setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-watch-lock"
        )

        ots_bytes = _ots_for(inv_digest)
        _seed_inv_completed_verify_job(sp_fake, sd, inv_digest, ots_bytes)
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        _register_attested(btc_fake.rpc, inv_digest)

        # Pre-acquire the staging .lock from a sibling FileLock instance.
        sibling = filelock.FileLock(str(sd / ".lock"))
        sibling.acquire(timeout=5)

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

        _inject_fake_zmq(monkeypatch, _ContendedStream())

        try:
            with caplog.at_level(logging.WARNING):
                r = runner.invoke(
                    app,
                    _stage_args(
                        empty_sha256_root,
                        staging,
                        "timestamp",
                        "inventory",
                        "obj:inv-watch-lock",
                        "--upgrade",
                        "--watch",
                    ),
                )
        finally:
            sibling.release()

        assert r.exit_code == 130, r.output
        # No torn checkpoint after lock contention + cancel.
        load_ots(sd)
        warn_msgs = [
            rec.getMessage().lower()
            for rec in caplog.records
            if rec.levelno == logging.WARNING
        ]
        assert any("lock" in m for m in warn_msgs), warn_msgs


# =========================================================================== #
# Layer-2: --retry  (mutually exclusive flag guards)
# =========================================================================== #


class TestInventoryRetryFlagCombinationGuards:
    @pytest.mark.parametrize("extra_flag", ["--upgrade", "--watch"])
    def test_retry_with_upgrade_or_watch_rejects(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        extra_flag: str,
    ) -> None:
        """``--retry --upgrade`` and ``--retry --watch`` rejected at CLI
        surface with non-zero exit and an actionable message naming both
        conflicting flags."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, _da, _inv_digest = _setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-retry-combo"
        )

        pre_calls = len(sp_fake._submissions)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "inventory",
                "obj:inv-retry-combo",
                "--retry",
                extra_flag,
            ),
        )
        assert r.exit_code != 0, r.output
        assert "--retry" in r.output, r.output
        assert extra_flag in r.output, r.output
        # Rejected before any SP POST.
        assert len(sp_fake._submissions) == pre_calls
        # Phase unchanged.
        assert load_ots(sd).phase is Phase.INVENTORY_PENDING


# =========================================================================== #
# Layer-2: --retry phase gate  (only legal at INVENTORY_PENDING)
# =========================================================================== #


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
                "timestamp",
                "inventory",
                "obj:inv-retry-phase",
                "--retry",
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
    def test_retry_at_inventory_pending_resubmits_inv_digest(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """At INVENTORY_PENDING with a populated ``inventory_proof``,
        ``--retry`` re-POSTs ``[inv_digest]`` to SP one more time; phase
        stays INVENTORY_PENDING. Single-digest semantics: the inventory
        side has no ``submissions`` list — a re-POST at INVENTORY_PENDING
        IS the "retry" (mirrors content --retry's "re-POST FAILED"
        intent). InventoryProof is not required to grow a status field."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, _da, inv_digest = _setup_at_inventory_pending(
            runner, empty_sha256_root, staging, "obj:inv-retry-ok"
        )

        # Setup ran `stage timestamp inventory` once → 1 SP submission.
        pre_calls = list(sp_fake._submissions)
        assert len(pre_calls) >= 1
        assert tuple(pre_calls[-1].digests) == (inv_digest,)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "inventory",
                "obj:inv-retry-ok",
                "--retry",
            ),
        )
        assert r.exit_code == 0, r.output

        new_calls = sp_fake._submissions[len(pre_calls) :]
        assert len(new_calls) == 1, new_calls
        assert tuple(new_calls[0].digests) == (inv_digest,)

        state = load_ots(sd)
        assert state.phase is Phase.INVENTORY_PENDING
        assert state.inventory_proof is not None
        assert state.inventory_proof.digest == inv_digest


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
                "timestamp",
                "inventory",
                "obj:rt-inv",
                "--upgrade",
            ),
        )
        assert r.exit_code == 0, r.output

        state = load_ots(sd)
        assert state.phase is Phase.INVENTORY_UPGRADED
        assert state.inventory_proof is not None
        proof = state.inventory_proof.proof
        assert proof.verified is True
        assert proof.block_height == attest_height
        assert proof.confirmations_at_verify is not None
        assert proof.confirmations_at_verify >= MIN_CONFS
        assert ots_cache_path(sd, inv_digest).read_bytes() == ots_bytes
