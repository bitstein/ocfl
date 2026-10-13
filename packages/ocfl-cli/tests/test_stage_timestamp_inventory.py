"""Tests for `stage timestamp` on the inventory target.

Pins the build-submit-serialize contract from `OTS workflow contract`:

- Precondition: phase ``content-upgraded`` (else ``PhaseError`` ⇒ exit 3
  with a next-step hint).
- Build the staged inventory via ``StorageRoot.plan`` (same path as
  ``stage commit``); the inventory's manifest now contains the ``<file>.ots``
  companion content files ingested by the prior ``--upgrade`` pass.
- Compute ``plan.inventory.digest`` and write an ``StagedInventory`` intent
  (``version=<head>``, ``digest=<inv_digest>``, ``job_id=None``) into
  ``ots.json``.
- POST a single-digest payload (the inventory digest) to SimpleProof via
  ``sp.submit_hashes([inv_digest], category_id)``.
- Save the ``CommitPlan`` to ``<staging>/plan.json`` (``CommitPlan.to_json``);
  round-trip via ``CommitPlan.from_json``. The inventory proof is attached
  at ``stage commit``, so the saved plan has none.
- Phase advances to ``inventory-pending``.

FSM (already encoded in ``_TRANSITIONS``):

- ``content-upgraded`` ⇒ advance to ``inventory-pending``
- ``inventory-pending`` ⇒ stay (resubmit; re-POSTs and re-serializes)
- ``inventory-upgraded`` ⇒ stay (noop; no SP call)
- All other phases ⇒ ``PhaseError``
"""

import hashlib
from pathlib import Path

import pytest
from fakes import FakeBitcoinRPC, FakeSimpleProof
from msgspec.structs import replace
from pydantic import SecretStr
from runner_helpers import runner_obj
from typer.testing import CliRunner

from ocfl_cli.config import Config
from ocfl_cli.config.ots import SimpleProofConfig
from ocfl_cli.main import app
from ocfl_cli.model.metadata import StagedMetadata, StagedUser
from ocfl_cli.model.ots_state import Proof, VerifyJob
from ocfl_cli.model.phase import Phase, TimestampTarget
from ocfl_cli.store.ots_state import load_ots, ots_cache_path, plan_path, save_ots
from ocfl_cli.store.staging import StagingStore
from ocfl_cli.workflow import timestamp as wf_timestamp
from ocfl_lib import CommitPlan, LocalFS, Spec, StorageRoot, get_layout
from tests.helpers.ots import build_attestation

# --------------------------------------------------------------------------- #
# Config + path helpers (mirrors test_stage_timestamp_content_upgrade.py;
# no shared-helpers extraction this round per scoping note).
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
    runner_obj(runner)["btc_rpc_factory"] = lambda cfg: fake


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
# Setup: bring an OTS staging to phase=content-upgraded with a real
# inventory containing both `a.txt` and its `a.txt.ots` companion.
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


def _setup_content_upgraded(
    runner: CliRunner,
    root: Path,
    staging: Path,
    object_id: str,
) -> tuple[Path, str, str]:
    """Build a staging at phase=content-upgraded with `a.txt` + `a.txt.ots`.

    Returns ``(staging_dir, content_digest, ots_companion_digest)``.

    Strategy: ingest both files via ``stage add`` (the `.ots` is opaque
    bytes — ``timestamp inventory`` doesn't parse it). After ingest,
    mutate ``ots.json`` to record the post-``--upgrade`` shape (phase +
    pending_digests + verified content_proof) so the FSM legality check
    at ``timestamp inventory`` passes. Phase mutation is the LAST write;
    ``stage add`` regresses phase to drafting per the FSM.
    """
    sd, _paths, digests = _new_ots_staging(
        runner, root, staging, object_id, ("a.txt", b"alpha")
    )
    da = digests["a.txt"]

    ots_src = staging.parent / "a.txt.ots"
    ots_src.write_bytes(b"\x00fake-ots-bytes-opaque-to-this-command")
    r = runner.invoke(app, _stage_args(root, staging, "add", object_id, str(ots_src)))
    assert r.exit_code == 0, r.output

    # Pin phase + populate pending_digests + proofs to mimic the
    # state the prior ``--upgrade`` pass would have left behind.
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
                block_height=800_000,
                confirmations_at_verify=3,
                verified_at="2026-04-23T16:30:00+00:00",
            )
        },
    )
    save_ots(sd, state)
    return sd, da, hashlib.sha256(ots_src.read_bytes()).hexdigest()


def _grow_chain_to(rpc: FakeBitcoinRPC, height: int) -> None:
    needed = height - rpc.tip_height
    if needed > 0:
        rpc.extend(needed)


def _register_attested(rpc: FakeBitcoinRPC, digest_hex: str) -> None:
    rpc.register_merkle_root(800_000, bytes.fromhex(digest_hex)[::-1].hex())


def _ots_for(digest_hex: str) -> bytes:
    return build_attestation(bytes.fromhex(digest_hex), block_height=800_000)


# =========================================================================== #
# Layer-2: inventory build includes .ots content files
# =========================================================================== #


class TestInventoryBuild:
    def test_inventory_build_includes_ots_content_files(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Manifest must contain both the data digest and the .ots companion
        digest after `stage timestamp inventory`."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, da, ots_digest = _setup_content_upgraded(
            runner, empty_sha256_root, staging, "obj:inv"
        )

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:inv",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == 0, r.output

        state = load_ots(sd)
        assert state.phase is Phase.INVENTORY_PENDING
        assert state.inventory is not None
        assert state.inventory_proof is not None

        # Re-load the on-disk plan and inspect its inventory's manifest.
        plan_bytes = plan_path(sd).read_bytes()
        plan2 = CommitPlan.from_json(plan_bytes)
        manifest = plan2.inventory.inventory.manifest
        assert da in manifest, f"data digest {da!r} missing from manifest"
        assert ots_digest in manifest, (
            f".ots companion digest {ots_digest!r} missing from manifest"
        )

        # inventory_proof.digest matches the plan digest; version matches head.
        assert state.inventory.digest == plan2.inventory.digest
        assert state.inventory.version == plan2.version
        # job_id stays None here; verify-batches is the upgrade pass's job.
        assert state.inventory.job_id is None

    def test_inventory_build_uses_staged_metadata(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, _da, _ots_digest = _setup_content_upgraded(
            runner, empty_sha256_root, staging, "obj:metadata"
        )

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:metadata",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == 0, r.output

        plan2 = CommitPlan.from_json(plan_path(sd).read_bytes())
        vi = plan2.inventory.inventory.versions[plan2.version]
        assert vi.message == "Timestamped inventory"
        assert vi.user is not None
        assert vi.user.name == "Alice"
        assert vi.user.address == "mailto:alice@example.org"


# --------------------------------------------------------------------------- #
# Service primitives for batch-size-1 inventory submission
# --------------------------------------------------------------------------- #


class TestInventorySubmissionPrimitives:
    def test_prepare_persists_intent_before_submit_posts(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, _da, _ots_digest = _setup_content_upgraded(
            runner, empty_sha256_root, staging, "obj:inv-primitive"
        )
        cfg = Config(
            simpleproof=SimpleProofConfig(
                base_url="https://stage.example",
                proof_category_id=641,
                access_token=SecretStr("tok"),
                api_key=SecretStr("key"),
            )
        )
        store = StagingStore(staging)
        sr = StorageRoot.open(LocalFS(empty_sha256_root))

        run = _run(cfg, store, sr, empty_sha256_root, "obj:inv-primitive", sd, sp_fake)
        prepared = wf_timestamp.prepare_submission_locked(
            run.area, category_config=641, runtime=run.runtime
        )

        # The intent and plan.json are durable before the POST; the phase
        # moves only once the digest is sent.
        (digest,) = prepared.digests
        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_UPGRADED
        assert state.inventory is not None
        assert state.inventory.digest == digest
        (intent,) = [s for s in state.submissions if s.target == "inventory"]
        assert (intent.status, intent.digests) == ("submitting", (digest,))
        assert not sp_fake._submissions
        assert plan_path(sd).is_file()

        result = wf_timestamp.submit_prepared_locked(run, prepared)

        assert result.submitted_count == 1
        assert len(sp_fake._submissions) == 1
        assert sp_fake._submissions[0].digests == (digest,)
        state = load_ots(sd)
        assert state.phase is Phase.INVENTORY_PENDING
        (sent,) = [s for s in state.submissions if s.target == "inventory"]
        assert sent.status == "submitted"

    def test_prepare_inventory_upgraded_is_noop(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, _da, _ots_digest = _setup_content_upgraded(
            runner, empty_sha256_root, staging, "obj:inv-primitive-noop"
        )
        save_ots(sd, replace(load_ots(sd), phase=Phase.INVENTORY_UPGRADED))
        cfg = Config(
            simpleproof=SimpleProofConfig(
                base_url="https://stage.example",
                proof_category_id=641,
                access_token=SecretStr("tok"),
                api_key=SecretStr("key"),
            )
        )
        store = StagingStore(staging)
        sr = StorageRoot.open(LocalFS(empty_sha256_root))

        run = _run(
            cfg, store, sr, empty_sha256_root, "obj:inv-primitive-noop", sd, sp_fake
        )
        prepared = wf_timestamp.prepare_submission_locked(
            run.area, category_config=641, runtime=run.runtime
        )
        result = wf_timestamp.submit_prepared_locked(run, prepared)

        assert prepared.digests == ()
        assert result.submitted_count == 0
        assert load_ots(sd).phase is Phase.INVENTORY_UPGRADED
        assert not sp_fake._submissions


def _run(cfg, store, sr, root, object_id, sd, sp_fake) -> wf_timestamp.TimestampRun:
    from fakes import FakeBitcoinRPC, InMemoryHashBlockStream, RecordingSleeper

    from ocfl_cli.ports import Runtime
    from ocfl_cli.system import SystemClock

    return wf_timestamp.TimestampRun(
        cfg=cfg,
        store=store,
        sr=sr,
        root_path=str(root.resolve()),
        object_id=object_id,
        staging_dir=sd,
        sp_factory=lambda cfg: sp_fake,
        btc_factory=lambda cfg: FakeBitcoinRPC(),
        runtime=Runtime(
            clock=SystemClock(),
            sleeper=RecordingSleeper(),
            block_streams=lambda cfg: InMemoryHashBlockStream(),
        ),
    )


class TestUnverifiedContentGate:
    def test_inventory_timestamp_blocked_when_content_proof_unverified(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A reorg can reset a content proof to verified=False while the phase
        stays content-upgraded. ``timestamp inventory`` must then refuse rather
        than anchor the inventory over an unverified content proof (exit 3) —
        the proof must be re-anchored via ``timestamp content --upgrade`` first.
        """
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, da, _ots_digest = _setup_content_upgraded(
            runner, empty_sha256_root, staging, "obj:reorg-gate"
        )
        # Simulate a reorg resetting the content proof; phase stays upgraded.
        state = load_ots(sd)
        state = replace(
            state,
            proofs={da: replace(state.proofs[da], verified=False)},
        )
        save_ots(sd, state)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:reorg-gate",
                "--expect",
                "inventory",
            ),
        )

        assert r.exit_code == 3, r.output
        assert "unverified" in r.output.lower()
        # No inventory work happened; phase held at content-upgraded.
        assert not plan_path(sd).exists()
        assert not sp_fake._submissions
        assert load_ots(sd).phase is Phase.CONTENT_UPGRADED

    def test_inventory_timestamp_blocked_after_reorg_reverify_resets_content(
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
            runner, empty_sha256_root, staging, "obj:reorg-real", ("a.txt", b"alpha")
        )
        d = digests["a.txt"]
        ots_bytes = _ots_for(d)
        cache = ots_cache_path(sd, d)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(ots_bytes)
        _grow_chain_to(btc_fake.rpc, 800_000 + 3 - 1)
        _register_attested(btc_fake.rpc, d)
        state = replace(
            load_ots(sd),
            phase=Phase.CONTENT_UPGRADED,
            pending_digests=(d,),
            verify_jobs=(
                VerifyJob(job_id="old-job", digests=(d,), status="COMPLETED"),
            ),
            proofs={
                d: Proof(
                    target=TimestampTarget.CONTENT,
                    ots_path=f"ots-cache/{d}.ots",
                    verified=True,
                    block_height=800_000,
                    confirmations_at_verify=3,
                    verified_at="2026-04-23T16:30:00+00:00",
                )
            },
        )
        save_ots(sd, state)

        btc_fake.rpc.reorg_at(800_000)
        _grow_chain_to(btc_fake.rpc, 800_000 + 3 - 1)
        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:reorg-real",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == 0, r.output
        assert load_ots(sd).proofs[d].verified is False

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:reorg-real",
                "--expect",
                "inventory",
            ),
        )

        assert r.exit_code == 3, r.output
        assert "unverified" in r.output.lower()
        assert not plan_path(sd).exists()
        assert load_ots(sd).phase is Phase.CONTENT_UPGRADED


class TestMetadataGate:
    def test_missing_staged_metadata_blocks_inventory_timestamp(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, _da, _ots_digest = _setup_content_upgraded(
            runner, empty_sha256_root, staging, "obj:missing-metadata"
        )
        store = StagingStore(staging)
        store.save_metadata(
            str(empty_sha256_root.resolve()),
            "obj:missing-metadata",
            StagedMetadata(user=StagedUser(name="Alice")),
        )

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:missing-metadata",
                "--expect",
                "inventory",
            ),
        )

        assert r.exit_code == 1
        assert "missing staged metadata" in r.output.lower()
        assert "message" in r.output
        assert "user.address" in r.output
        assert not plan_path(sd).exists()
        assert not sp_fake._submissions

    def test_missing_metadata_file_blocks_inventory_timestamp(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, _da, _ots_digest = _setup_content_upgraded(
            runner, empty_sha256_root, staging, "obj:missing-metadata-file"
        )
        (sd / "metadata.json").unlink()

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:missing-metadata-file",
                "--expect",
                "inventory",
            ),
        )

        assert r.exit_code == 1
        assert "missing staged metadata" in r.output.lower()
        assert "message" in r.output
        assert "user.name" in r.output
        assert "user.address" in r.output
        assert not plan_path(sd).exists()
        assert not sp_fake._submissions

    def test_allow_missing_metadata_bypasses_inventory_timestamp_gate(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, _da, _ots_digest = _setup_content_upgraded(
            runner, empty_sha256_root, staging, "obj:allow-metadata"
        )
        store = StagingStore(staging)
        store.save_metadata(
            str(empty_sha256_root.resolve()),
            "obj:allow-metadata",
            StagedMetadata(),
        )

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:allow-metadata",
                "--allow-missing-metadata",
                "--expect",
                "inventory",
            ),
        )

        assert r.exit_code == 0, r.output
        assert load_ots(sd).phase is Phase.INVENTORY_PENDING
        assert plan_path(sd).is_file()

    @pytest.mark.parametrize(
        "verb_args",
        [
            ("upgrade",),
            ("upgrade", "--watch"),
            ("retry",),
        ],
    )
    def test_allow_missing_metadata_is_only_a_timestamp_option(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        verb_args: tuple[str, ...],
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        _setup_content_upgraded(
            runner, empty_sha256_root, staging, "obj:irrelevant-flag"
        )

        verb, *flags = verb_args
        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                verb,
                "obj:irrelevant-flag",
                *flags,
                "--allow-missing-metadata",
            ),
        )

        assert r.exit_code == 2
        assert "--allow-missing-metadata" in r.output


# =========================================================================== #
# Layer-2: plan.json round-trip; no inventory proof yet
# =========================================================================== #


class TestPlanRoundTrip:
    def test_plan_json_round_trip_has_no_inventory_proof_yet(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, _da, _ots_digest = _setup_content_upgraded(
            runner, empty_sha256_root, staging, "obj:rt"
        )

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:rt",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == 0, r.output

        raw = plan_path(sd).read_bytes()
        plan2 = CommitPlan.from_json(raw)

        state = load_ots(sd)
        assert state.inventory is not None
        # Round-trips the inventory digest exactly.
        assert plan2.inventory.digest == state.inventory.digest
        # The inventory proof is attached at `stage commit`, not at this step.
        assert plan2.inventory_proof is None


# =========================================================================== #
# Layer-2: PhaseError from wrong phase carries a next-step hint
# =========================================================================== #


class TestPhaseGate:
    def test_phase_error_at_drafting_with_hint(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fresh OTS staging (phase=drafting) ⇒ exit 3 with a hint."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        _new_ots_staging(
            runner, empty_sha256_root, staging, "obj:err-draft", ("a.txt", b"alpha")
        )

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:err-draft",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == 3, r.output
        out = (r.output or "").lower()
        # Either name the wrong phase, or hint at the next legal step.
        assert "drafting" in out or "content" in out, r.output

    def test_phase_error_at_content_pending_with_hint(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Phase=content-pending ⇒ exit 3 with a hint."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, _paths, _digests = _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:err-pend",
            ("a.txt", b"alpha"),
        )
        save_ots(sd, replace(load_ots(sd), phase=Phase.CONTENT_PENDING))

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:err-pend",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == 3, r.output
        out = (r.output or "").lower()
        assert "content-pending" in out or "content" in out, r.output


# =========================================================================== #
# Layer-2: idempotent resubmit at inventory-pending (stay # resubmit)
# =========================================================================== #


class TestResubmitAtInventoryPending:
    def test_rerun_at_inventory_pending_sends_nothing(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Per FSM matrix `_STAY`: a second invocation at `inventory-pending`
        sends nothing; the submitted digest keeps its requested proof
        (ADR-0004), so it is not POSTed again."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, _da, _ots_digest = _setup_content_upgraded(
            runner, empty_sha256_root, staging, "obj:resubmit"
        )

        # First call: advances to inventory-pending; one SP submission.
        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:resubmit",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == 0, r.output
        s1 = load_ots(sd)
        assert s1.phase is Phase.INVENTORY_PENDING
        assert s1.inventory is not None
        assert len(sp_fake._submissions) == 1
        first_inv_digest = s1.inventory.digest
        assert sp_fake._submissions[0].digests == (first_inv_digest,)

        # Second call: stays at inventory-pending; nothing is sent.
        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:resubmit",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == 0, r.output
        assert load_ots(sd).phase is Phase.INVENTORY_PENDING
        assert len(sp_fake._submissions) == 1
        assert load_ots(sd).inventory == s1.inventory


# =========================================================================== #
# Layer-2: noop at inventory-upgraded (matrix `_STAY` # noop)
# =========================================================================== #


class TestNoopAtInventoryUpgraded:
    def test_noop_at_inventory_upgraded(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """At `inventory-upgraded` (after the upgrade pass verified the
        inventory proof): re-running is a noop — no SP submission, phase
        unchanged."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, _da, _ots_digest = _setup_content_upgraded(
            runner, empty_sha256_root, staging, "obj:noop"
        )

        # Run once to land at inventory-pending, then mutate to
        # inventory-upgraded (simulates the upgrade pass having verified).
        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:noop",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == 0, r.output
        assert load_ots(sd).phase is Phase.INVENTORY_PENDING
        save_ots(sd, replace(load_ots(sd), phase=Phase.INVENTORY_UPGRADED))

        pre_calls = len(sp_fake._submissions)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:noop",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == 0, r.output
        assert load_ots(sd).phase is Phase.INVENTORY_UPGRADED
        assert len(sp_fake._submissions) == pre_calls
