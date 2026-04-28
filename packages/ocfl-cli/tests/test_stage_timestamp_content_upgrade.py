"""Tests for `ocfl stage timestamp content --upgrade` (OTS workflow step 11).

Pins step 11 of `OTS workflow contract`:

- For each ``Submission(status="submitted")`` in ``ots.json`` with no
  associated ``VerifyJob`` covering its digests, call
  ``sp.verify_batch(digests)`` and append a ``VerifyJob`` to
  ``state.verify_jobs``.
- Poll each outstanding ``VerifyJob`` via ``sp.poll_job``.
- On ``COMPLETED``: download via ``sp.download_results``; for every digest
  parse the ``.ots`` and call ``verify_attestation(rpc, ots_bytes,
  expected_digest=bytes.fromhex(d), min_confirmations=cfg.bitcoin.confirmations)``.
- On ``VerifiedAttestation`` apply the per-digest crash-recovery invariant:
  (1) atomic write ``ots-cache/<d>.ots``,
  (2) ``ingest_file`` cached bytes into stage state at ``<obj_rel>.ots``
      (updating ``content_map``),
  (3) atomic update ``ots.json.content_proofs[d] = VerifiedProof(verified=True,
      block_height=..., confirmations_at_verify=..., verified_at=...)``.
- On ``PendingVerification`` cache bytes (step 1), record ``content_proofs[d]
  = VerifiedProof(verified=False)`` (no ingest, no advance).
- ``ReorgDetectedError`` / ``AttestationMismatchError`` HALT the upgrade
  (CLI exit 5); ``ots.json`` left intact.
- Phase advances to ``content-upgraded`` only when both:
  (1) ∀ d ∈ pending_digests: content_proofs[d].verified is True
  (2) non_ots_digests(stage.state) ⊆ pending_digests
- Reorg reconciliation (`_reverify_existing_proofs`) at head of every
  --upgrade pass: for each verified=True proof, re-parse cached `.ots`
  and compare attested block hash against ``btc.getblockhash(block_height)``;
  on mismatch reset ``verified=False`` and re-fetch via SP.

Tests inject ``FakeBitcoinRPC`` / ``FakeSimpleProof`` via
``runner.obj["btc_rpc_factory"]`` / ``runner.obj["sp_client_factory"]``
(see ``conftest.py::runner`` for the auto-attach mechanism).
"""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from fakes import FakeBitcoinRPC, FakeSimpleProof
from typer.testing import CliRunner

from ocfl_cli.main import app
from ocfl_cli.stage.fsm import Phase
from ocfl_cli.stage.state import (
    Submission,
    VerifyJob,
    load_ots,
    ots_cache_path,
    save_ots,
)
from ocfl_cli.stage.store import StagingStore
from ocfl_lib import LocalFS, Spec, StorageRoot, get_layout
from tests.helpers.ots import build_attestation

# --------------------------------------------------------------------------- #
# Config + path helpers
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
    runner.obj["sp_client_factory"] = lambda cfg: fake


def _inject_fake_btc(runner: CliRunner, fake: FakeBitcoinRPC) -> None:
    """Inject *fake* via ``runner.obj["btc_rpc_factory"]``."""
    runner.obj["btc_rpc_factory"] = lambda cfg: fake


# --------------------------------------------------------------------------- #
# Storage-root + staging fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def empty_sha256_root(tmp_path: Path) -> Path:
    location = tmp_path / "root"
    location.mkdir()
    fs = LocalFS(location)
    layout = get_layout("0002-flat-direct-storage-layout")
    StorageRoot.init(fs, fs, spec=Spec.V1_1, layout=layout)
    return location


# --------------------------------------------------------------------------- #
# .ots-bytes + chain helpers
# --------------------------------------------------------------------------- #

DIGEST_BLOCK_HEIGHT = 800_000
MIN_CONFS = 3


def _ots_for(digest_hex: str, *, block_height: int = DIGEST_BLOCK_HEIGHT) -> bytes:
    """Build synthetic .ots bytes committing to *digest_hex* at *block_height*."""
    return build_attestation(bytes.fromhex(digest_hex), block_height=block_height)


def _ots_pending(digest_hex: str) -> bytes:
    """Build synthetic .ots bytes with only a PendingAttestation (no block)."""
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
    """Register the merkle root in the fake chain so verify_attestation succeeds.

    `build_attestation` puts no ops between the leaf and the
    BitcoinBlockHeaderAttestation — attested merkle root (internal bytes) ==
    the file digest. bitcoind reports merkle in display (reversed) hex.
    """
    rpc.register_merkle_root(height, bytes.fromhex(digest_hex)[::-1].hex())


# --------------------------------------------------------------------------- #
# Setup helper: create OTS staging at content-pending with a single submitted
# digest, optionally pre-load a COMPLETED verify_job covering it.
# --------------------------------------------------------------------------- #


def _new_ots_staging(
    runner: CliRunner,
    root: Path,
    staging: Path,
    object_id: str,
    *files: tuple[str, bytes],
) -> tuple[Path, dict[str, str], dict[str, str]]:
    """Run ``stage new --ots`` + ``stage add`` for each file.

    Returns ``(staging_dir, path_by_seed, digest_by_seed)``.
    Each *file* is ``(seed_name, bytes)`` written to a tmp path; logical
    path is ``seed_name`` (e.g. ``"a.txt"``).
    """
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
    """Pin ots.json into phase=content-pending with a submitted Submission."""
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
                job_id=None,
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
    """Pre-create a COMPLETED job on the fake + record IN_PROGRESS in ots.json.

    The upgrade pass should detect the existing verify_job (no second
    POST), poll, see COMPLETED, then download + verify.
    """
    job_id = sp_fake.verify_batch(list(digests))
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
# Layer-2: --upgrade happy path  (verified, ingested, phase advances)
# =========================================================================== #


class TestUpgradeHappyPath:
    def test_single_digest_at_3_confs_advances_to_content_upgraded(
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
        sd, _paths, digests = _new_ots_staging(
            runner, empty_sha256_root, staging, "obj:up", ("a.txt", b"alpha")
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes = _ots_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})

        # Tip exactly N confirmations deep past attested height.
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        _register_attested(btc_fake.rpc, d)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "content",
                "obj:up",
                "--upgrade",
            ),
        )
        assert r.exit_code == 0, r.output

        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_UPGRADED
        proof = state.content_proofs[d]
        assert proof.verified is True
        assert proof.block_height == DIGEST_BLOCK_HEIGHT
        assert proof.confirmations_at_verify is not None
        assert proof.confirmations_at_verify >= MIN_CONFS
        assert proof.verified_at is not None
        # ots_path is staging-relative.
        assert proof.ots_path == f"ots-cache/{d}.ots"

        # Cache bytes match what SP returned.
        assert ots_cache_path(sd, d).read_bytes() == ots_bytes

        # Inventory has the .ots companion ingested at "<obj_rel>.ots".
        store = StagingStore(staging)
        root_path = str(empty_sha256_root.resolve())
        inv = store.load_inventory(root_path, "obj:up")
        all_paths = {p for ps in inv.versions[inv.head].state.values() for p in ps}
        assert "a.txt" in all_paths
        assert "a.txt.ots" in all_paths
        # content_map gained an entry for the .ots digest.
        cm = store.load_content_map(root_path, "obj:up")
        ots_digest = hashlib.sha256(ots_bytes).hexdigest()
        assert ots_digest in cm

    def test_multi_digest_partial_pending_does_not_advance(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two digests submitted; only one verifies → phase stays content-pending."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _paths, digests = _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:multi",
            ("a.txt", b"alpha"),
            ("b.txt", b"bravo"),
        )
        da, db = digests["a.txt"], digests["b.txt"]
        _force_content_pending(sd, (da, db))

        ots_a = _ots_for(da)  # buried — verifies
        ots_b = _ots_pending(db)  # pending attestation — held
        _seed_completed_verify_job(sp_fake, sd, (da, db), {da: ots_a, db: ots_b})

        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        _register_attested(btc_fake.rpc, da)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "content",
                "obj:multi",
                "--upgrade",
            ),
        )
        assert r.exit_code == 0, r.output

        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_PENDING
        assert state.content_proofs[da].verified is True
        assert state.content_proofs[db].verified is False
        # Both .ots bytes cached, even the pending one.
        assert ots_cache_path(sd, da).is_file()
        assert ots_cache_path(sd, db).is_file()


# =========================================================================== #
# Layer-2: 3-conf gate
# =========================================================================== #


class TestThreeConfGate:
    @pytest.mark.parametrize(
        ("confs_to_grow", "expect_advance"),
        [
            (1, False),  # 1 confirmation: held
            (2, False),  # 2 confirmations: held
            (3, True),  # 3 confirmations: verifies + advances
            (4, True),  # deeper than gate: still verifies
        ],
    )
    def test_advance_only_at_or_above_min_confirmations(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        confs_to_grow: int,
        expect_advance: bool,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _paths, digests = _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:gate",
            ("a.txt", b"alpha"),
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes = _ots_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})

        # confirmations_at_height = tip - height + 1.
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + confs_to_grow - 1)
        _register_attested(btc_fake.rpc, d)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "content",
                "obj:gate",
                "--upgrade",
            ),
        )
        assert r.exit_code == 0, r.output

        state = load_ots(sd)
        if expect_advance:
            assert state.phase is Phase.CONTENT_UPGRADED
            assert state.content_proofs[d].verified is True
        else:
            assert state.phase is Phase.CONTENT_PENDING
            # Insufficient-confs path still records a (verified=False) proof
            # entry alongside the cached bytes.
            assert ots_cache_path(sd, d).is_file()
            assert state.content_proofs[d].verified is False


# =========================================================================== #
# Layer-2: pending attestation held until confirmations reached
# =========================================================================== #


class TestPendingHeld:
    def test_pending_attestation_held_then_resolves_on_next_pass(
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
        sd, _paths, digests = _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:pend",
            ("a.txt", b"alpha"),
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        # Round 1: SP returns a PendingAttestation-only .ots.
        pending_bytes = _ots_pending(d)
        job_id = _seed_completed_verify_job(sp_fake, sd, (d,), {d: pending_bytes})
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "content",
                "obj:pend",
                "--upgrade",
            ),
        )
        assert r.exit_code == 0, r.output
        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_PENDING
        assert state.content_proofs[d].verified is False
        assert ots_cache_path(sd, d).is_file()

        # Round 2: SP now serves a Bitcoin-anchored .ots; re-poll converges.
        anchored = _ots_for(d)
        sp_fake._results[f"results://{job_id}"] = {d: anchored}
        _register_attested(btc_fake.rpc, d)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "content",
                "obj:pend",
                "--upgrade",
            ),
        )
        assert r.exit_code == 0, r.output
        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_UPGRADED
        assert state.content_proofs[d].verified is True


# =========================================================================== #
# Layer-2: reorg shallower than N → absorbed (verified→False on next pass)
# =========================================================================== #


class TestReorgShallow:
    def test_reorg_of_attested_block_within_n_marks_unverified(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """First pass: verifies. Reorg replaces the attested block (shallow,
        still within N of tip). Second pass: ``_reverify_existing_proofs``
        notices block-hash mismatch and resets verified=False — no HALT."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _paths, digests = _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:reorg-s",
            ("a.txt", b"alpha"),
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes = _ots_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})

        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        _register_attested(btc_fake.rpc, d)

        # Round 1: verifies + advances.
        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "content",
                "obj:reorg-s",
                "--upgrade",
            ),
        )
        assert r.exit_code == 0, r.output
        assert load_ots(sd).content_proofs[d].verified is True

        # Reorg the attested height: block_hash changes, height still in chain
        # (within N). The cached .ots's embedded merkle path is now stale.
        btc_fake.rpc.reorg_at(DIGEST_BLOCK_HEIGHT)
        # New block at this height has a DIFFERENT merkle root — the original
        # proof no longer matches; do NOT re-register the old merkle.

        # Round 2: --upgrade re-verifies existing proofs at head.
        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "content",
                "obj:reorg-s",
                "--upgrade",
            ),
        )
        assert r.exit_code == 0, r.output
        state = load_ots(sd)
        # Reorg absorbed: proof was reset, no HALT.
        assert state.content_proofs[d].verified is False


# =========================================================================== #
# Layer-2: reorg deeper than N → ReorgDetectedError (HALT, exit 5)
# =========================================================================== #


class TestReorgDeep:
    def test_reorg_deeper_than_n_halts_with_exit_5(
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
        sd, _paths, digests = _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:reorg-d",
            ("a.txt", b"alpha"),
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes = _ots_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})

        # Tip well behind attested height (more than MIN_CONFS short).
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT - MIN_CONFS - 5)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "content",
                "obj:reorg-d",
                "--upgrade",
            ),
        )
        assert r.exit_code == 5, r.output
        # ots.json untouched at HALT — phase still content-pending.
        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_PENDING


# =========================================================================== #
# Layer-2: merkle mismatch → AttestationMismatchError (HALT, exit 5)
# =========================================================================== #


class TestMerkleMismatch:
    def test_wrong_merkle_root_halts_with_exit_5(
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
        sd, _paths, digests = _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:mm",
            ("a.txt", b"alpha"),
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes = _ots_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})

        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        # Register a WRONG merkle root at the attested height.
        btc_fake.rpc.register_merkle_root(DIGEST_BLOCK_HEIGHT, "00" * 32)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "content",
                "obj:mm",
                "--upgrade",
            ),
        )
        assert r.exit_code == 5, r.output
        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_PENDING
        # ots.json's content_proofs unchanged for d (no verified=True written).
        assert d not in state.content_proofs or not state.content_proofs[d].verified


# =========================================================================== #
# Layer-2: crash-recovery parametrized fault injection
# =========================================================================== #


class TestCrashRecovery:
    """Inject a fault at each step boundary; assert restart converges.

    Boundaries tested:
      - ``before_cache``: fault before atomic cache write
      - ``after_cache``: cache written, ingest_file fails
      - ``after_ingest``: cache + ingest done, ots.json save fails
      - ``during_ots_save``: ots.json save raises mid-write (atomicity guards)

    For each: first --upgrade pass crashes (non-zero exit); a second pass
    with no fault converges to phase=content-upgraded, content_proofs[d]
    .verified=True, cached bytes intact, .ots companion ingested.
    """

    @pytest.mark.parametrize(
        "fault_at",
        ["before_cache", "after_cache", "after_ingest", "during_ots_save"],
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
        sd, _paths, digests = _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:crash",
            ("a.txt", b"alpha"),
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes = _ots_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        _register_attested(btc_fake.rpc, d)

        # Inject the configured fault.
        _install_crash_fault(monkeypatch, fault_at, sd, d)

        # Round 1: expect non-zero exit (crash).
        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "content",
                "obj:crash",
                "--upgrade",
            ),
        )
        assert r.exit_code != 0, r.output
        # Crash-recovery invariant: ots.json is never torn (always parses).
        load_ots(sd)
        # If verified=True is recorded for d, both cache + ingest must have
        # completed (the implication direction of the invariant).
        post_crash = load_ots(sd)
        if (
            d in post_crash.content_proofs
            and post_crash.content_proofs[d].verified is True
        ):
            assert ots_cache_path(sd, d).is_file()
            store = StagingStore(staging)
            inv = store.load_inventory(str(empty_sha256_root.resolve()), "obj:crash")
            paths = {p for ps in inv.versions[inv.head].state.values() for p in ps}
            assert "a.txt.ots" in paths

        # Remove the fault; a second pass must converge.
        _uninstall_crash_fault(monkeypatch)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "content",
                "obj:crash",
                "--upgrade",
            ),
        )
        assert r.exit_code == 0, r.output
        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_UPGRADED
        assert state.content_proofs[d].verified is True
        # Cache bytes are correct (digest-keyed; survives multiple writes).
        assert ots_cache_path(sd, d).read_bytes() == ots_bytes
        # .ots companion present in staged inventory.
        store = StagingStore(staging)
        inv = store.load_inventory(str(empty_sha256_root.resolve()), "obj:crash")
        paths = {p for ps in inv.versions[inv.head].state.values() for p in ps}
        assert "a.txt.ots" in paths


_CRASH_FAULT_RESTORERS: list = []


def _patch_attr(target: object, attr: str, value: object) -> None:
    """Set *attr* on *target* selectively-undoably (no monkeypatch)."""
    sentinel = object()
    original = getattr(target, attr, sentinel)
    setattr(target, attr, value)
    if original is sentinel:
        _CRASH_FAULT_RESTORERS.append(lambda: delattr(target, attr))
    else:
        _CRASH_FAULT_RESTORERS.append(lambda: setattr(target, attr, original))


def _install_crash_fault(
    monkeypatch: pytest.MonkeyPatch,
    fault_at: str,
    sd: Path,
    digest: str,
) -> None:
    """Wrap module-level seams to raise at the specified boundary.

    `_atomic_write_bytes` from `ocfl_cli.stage.store` underlies BOTH the
    ots-cache write (step 1) and the ots.json save (step 3). Fault site
    is selected by inspecting the target path. `ingest_file` from
    `ocfl_cli.stage.ingest` is the step-2 seam.
    """
    from ocfl_cli.stage import ingest as ingest_mod
    from ocfl_cli.stage import store as staging_mod

    real_atomic = staging_mod._atomic_write_bytes

    cache_path = ots_cache_path(sd, digest)
    ots_json_path = sd / "ots.json"

    if fault_at == "before_cache":

        def _atomic(path: Path, data: bytes) -> None:
            if path == cache_path:
                raise OSError("injected: cache write failed")
            real_atomic(path, data)

        _patch_attr(staging_mod, "_atomic_write_bytes", _atomic)

    elif fault_at == "after_cache":

        def _ingest(*args: object, **kwargs: object):
            raise OSError("injected: ingest_file failed")

        # The implementer is expected to import ingest_file at the module
        # level of `commands.stage` so it's patchable there. Fall back to
        # patching the source module if it's referenced via attribute.
        _patch_attr(ingest_mod, "ingest_file", _ingest)
        try:
            from ocfl_cli.stage import service as stage_cmd

            _patch_attr(stage_cmd, "ingest_file", _ingest)
        except ImportError:
            pass

    elif fault_at == "after_ingest":

        def _atomic(path: Path, data: bytes) -> None:
            if path == ots_json_path:
                # Cache + ingest already done; fail the per-digest ots.json
                # update step — but only when the about-to-be-written state
                # carries verified=True for *digest* (so we don't crash on
                # earlier ots.json saves like verify_jobs init).
                try:
                    parsed = json.loads(data)
                    cp = parsed.get("content_proofs") or {}
                    entry = cp.get(digest)
                    if entry and entry.get("verified") is True:
                        raise OSError("injected: ots.json save failed")
                except json.JSONDecodeError, AttributeError:
                    pass
            real_atomic(path, data)

        _patch_attr(staging_mod, "_atomic_write_bytes", _atomic)

    elif fault_at == "during_ots_save":
        # Fault inside the atomic helper between temp-write and replace.
        # Old ots.json must remain intact (atomicity guarantee).
        original_replace = __import__("os").replace

        def _atomic(path: Path, data: bytes) -> None:
            if path == ots_json_path:
                try:
                    parsed = json.loads(data)
                    cp = parsed.get("content_proofs") or {}
                    entry = cp.get(digest)
                    if entry and entry.get("verified") is True:
                        # Write tmp, then crash before replace.
                        import os as _os

                        tmp = path.with_name(f"{path.name}.tmp.{_os.getpid()}")
                        with open(tmp, "wb") as f:
                            f.write(data)
                            f.flush()
                            _os.fsync(f.fileno())
                        # Leave tmp behind; raise before os.replace.
                        raise OSError("injected: crash mid-save")
                except json.JSONDecodeError, AttributeError:
                    pass
            real_atomic(path, data)
            assert original_replace  # silence unused-warning

        _patch_attr(staging_mod, "_atomic_write_bytes", _atomic)

    else:
        raise AssertionError(f"unknown fault_at: {fault_at!r}")


def _uninstall_crash_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo every monkeypatch installed by ``_install_crash_fault``."""
    while _CRASH_FAULT_RESTORERS:
        _CRASH_FAULT_RESTORERS.pop()()


# =========================================================================== #
# Layer-2: orphan-guard
# =========================================================================== #


class TestOrphanGuard:
    def test_mid_flight_stage_add_blocks_phase_advance(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """While at content-pending, ``stage add`` lands a NEW digest with
        no submission. ``--upgrade`` verifies the existing pending digest
        but MUST NOT advance: orphan in stage state outside pending_digests.
        """
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _paths, digests = _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:orphan",
            ("a.txt", b"alpha"),
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes = _ots_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        _register_attested(btc_fake.rpc, d)

        # Mid-flight add of a NEW file (not in pending_digests).
        new_src = tmp_path / "b.txt"
        new_src.write_bytes(b"bravo")
        r = runner.invoke(
            app,
            _stage_args(empty_sha256_root, staging, "add", "obj:orphan", str(new_src)),
        )
        assert r.exit_code == 0, r.output

        # `stage add` regresses phase per the FSM (CONTENT_PENDING/add ⇒ stay)
        # — re-pin to content-pending for this contract test (the stage_add
        # hook may or may not regress; we test the orphan-guard isolation).
        state = load_ots(sd)
        if state.phase is not Phase.CONTENT_PENDING:
            save_ots(sd, replace(state, phase=Phase.CONTENT_PENDING))

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "content",
                "obj:orphan",
                "--upgrade",
            ),
        )
        assert r.exit_code == 0, r.output
        state = load_ots(sd)
        # Orphan-guard: phase MUST NOT advance even though d verifies.
        assert state.phase is Phase.CONTENT_PENDING
        # Existing digest still verifies (work isn't wasted).
        assert state.content_proofs[d].verified is True
        # Orphan digest absent from pending_digests (it never joined).
        d_b = hashlib.sha256(b"bravo").hexdigest()
        assert d_b not in state.pending_digests


# =========================================================================== #
# Layer-2: phase advance only when both invariant conditions hold
# =========================================================================== #


class TestPhaseAdvanceInvariant:
    def test_advance_when_both_conditions_hold(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both conditions hold: pending fully verified + no orphans."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _paths, digests = _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:inv-ok",
            ("a.txt", b"alpha"),
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
                "timestamp",
                "content",
                "obj:inv-ok",
                "--upgrade",
            ),
        )
        assert r.exit_code == 0, r.output
        assert load_ots(sd).phase is Phase.CONTENT_UPGRADED

    def test_no_advance_when_condition1_fails_pending_unverified(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cond (1) fails: at least one pending digest unverified → no advance."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _paths, digests = _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:inv-1",
            ("a.txt", b"alpha"),
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        # Insufficient confs → not yet verified.
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: _ots_for(d)})
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT)  # 1 conf only
        _register_attested(btc_fake.rpc, d)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "content",
                "obj:inv-1",
                "--upgrade",
            ),
        )
        assert r.exit_code == 0, r.output
        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_PENDING
        assert state.content_proofs[d].verified is False

    def test_no_advance_when_condition2_fails_orphan_present(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cond (2) fails: stage state has a non-.ots digest not in
        pending_digests → no advance even if all pending verified."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _paths, digests = _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:inv-2",
            ("a.txt", b"alpha"),
            ("b.txt", b"bravo"),
        )
        da = digests["a.txt"]
        # Pin only `a.txt` into pending_digests (b is the orphan).
        _force_content_pending(sd, (da,))

        ots_a = _ots_for(da)
        _seed_completed_verify_job(sp_fake, sd, (da,), {da: ots_a})
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        _register_attested(btc_fake.rpc, da)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "content",
                "obj:inv-2",
                "--upgrade",
            ),
        )
        assert r.exit_code == 0, r.output
        state = load_ots(sd)
        # Cond (1) holds (a verified), but b is in stage state outside pending.
        assert state.content_proofs[da].verified is True
        assert state.phase is Phase.CONTENT_PENDING


# =========================================================================== #
# Layer-2: idempotent re-verify of an already-verified proof
# =========================================================================== #


class TestReverifyIdempotent:
    def test_second_upgrade_pass_with_no_changes_is_noop(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--upgrade`` already at content-upgraded with stable chain → no-op."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _paths, digests = _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:idem",
            ("a.txt", b"alpha"),
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes = _ots_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        _register_attested(btc_fake.rpc, d)

        # First pass: advances.
        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "content",
                "obj:idem",
                "--upgrade",
            ),
        )
        assert r.exit_code == 0, r.output
        assert load_ots(sd).phase is Phase.CONTENT_UPGRADED

        # Second pass: chain unchanged → noop, still content-upgraded.
        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "content",
                "obj:idem",
                "--upgrade",
            ),
        )
        assert r.exit_code == 0, r.output
        assert load_ots(sd).phase is Phase.CONTENT_UPGRADED


# =========================================================================== #
# Layer-3: regtest end-to-end (skipped without --run-bitcoin)
# =========================================================================== #


@pytest.mark.bitcoin
class TestRegtestEndToEnd:
    def test_real_rpc_real_attestation_with_invalidateblock_reorg(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        bitcoind_regtest,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Real bitcoind regtest, real OTS bytes, real RPC verify; then
        ``invalidateblock`` on the attestation block forces the next
        ``--upgrade`` pass to mark the proof ``verified=False`` via
        ``_reverify_existing_proofs``."""
        from tests.helpers.ots import build_regtest_attestation
        from tests.helpers.regtest import bitcoin_config_toml, bury_to_confs

        cfg_body = _SP_OK + bitcoin_config_toml(bitcoind_regtest, confirmations=3)
        _write_config(tmp_path, monkeypatch, body=cfg_body)
        _inject_fake_sp(runner, sp_fake)
        # Production btc_rpc_factory path used; no btc fake.

        staging = tmp_path / "staging"
        sd, _paths, digests = _new_ots_staging(
            runner, empty_sha256_root, staging, "obj:rt", ("a.txt", b"alpha")
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        digest_bytes = bytes.fromhex(d)
        ots_bytes, attest_height, attest_hash = build_regtest_attestation(
            bitcoind_regtest, digest_bytes
        )
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})
        bury_to_confs(bitcoind_regtest, attest_height, MIN_CONFS, "ocfltest")

        # First pass: verify + ingest + advance phase.
        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "content",
                "obj:rt",
                "--upgrade",
            ),
        )
        assert r.exit_code == 0, r.output
        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_UPGRADED
        assert state.content_proofs[d].verified is True
        assert state.content_proofs[d].block_height == attest_height

        # Reorg: invalidate the attestation block. Mine a longer fork so
        # the chain advances past it without the OP_RETURN tx — the cached
        # `.ots`'s merkle path no longer matches the chain at that height.
        bitcoind_regtest.rpc("invalidateblock", [attest_hash])
        addr = bitcoind_regtest.wallet_rpc("ocfltest", "getnewaddress")
        bitcoind_regtest.rpc("generatetoaddress", [MIN_CONFS + 2, addr])

        # SP re-fetch path: the upgrade pass calls verify-batches again
        # for the affected digest. Pre-seed a fresh COMPLETED job so the
        # call resolves; for this assertion we need only that the proof
        # gets reset to verified=False (not that it re-converges).
        sp_fake.advance_to_completed(sp_fake.verify_batch([d]), {d: ots_bytes})

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "content",
                "obj:rt",
                "--upgrade",
            ),
        )
        # Reorg reconciliation marks proof unverified; phase regresses.
        post = load_ots(sd)
        assert (
            post.content_proofs[d].verified is False
            or post.phase is not Phase.CONTENT_UPGRADED
        ), (
            f"reorg should have invalidated the proof; phase={post.phase} "
            f"verified={post.content_proofs[d].verified} exit={r.exit_code}"
        )
