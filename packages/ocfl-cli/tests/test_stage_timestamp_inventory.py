import hashlib
from pathlib import Path
from typing import NamedTuple

import pytest
from fakes import FakeBitcoinRPC, FakeSimpleProof
from msgspec.structs import replace
from ots_helpers import (
    MIN_CONFS,
    VERSION_METADATA,
    bury,
    final_proof_for,
    inject_fake_btc,
    inject_fake_sp,
    new_ots_staging,
    register_attested,
    replace_attested_block,
    verified_content_proof,
    write_config,
)
from pydantic import SecretStr
from runner_helpers import ObjRunner, stage_args

from ocfl_cli.config import Config
from ocfl_cli.config.ots import SimpleProofConfig
from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app
from ocfl_cli.model.metadata import StagedMetadata, StagedUser
from ocfl_cli.model.ots_state import VerifyJob
from ocfl_cli.model.phase import Phase
from ocfl_cli.store.ots_state import (
    load_ots,
    ots_cache_path,
    plan_path,
    save_ots,
    with_lock,
)
from ocfl_cli.store.staging import StagingStore
from ocfl_cli.workflow import timestamp as wf_timestamp
from ocfl_lib import CommitPlan, LocalFS, StorageRoot
from tests.helpers.faults import Crash, CrashBoundary


class ContentUpgradedStaging(NamedTuple):
    staging_dir: Path
    content_digest: str
    ots_companion_digest: str


def _setup_content_upgraded(
    runner: ObjRunner,
    root: Path,
    staging: Path,
    object_id: str,
) -> ContentUpgradedStaging:
    sd, digests = new_ots_staging(
        runner, root, staging, object_id, ("a.txt", b"alpha"), new_args=VERSION_METADATA
    )
    da = digests["a.txt"]

    ots_src = staging.parent / "a.txt.ots"
    ots_src.write_bytes(b"\x00fake-ots-bytes-opaque-to-this-command")
    r = runner.invoke(app, stage_args(root, staging, "add", object_id, str(ots_src)))
    assert r.exit_code == ExitCode.SUCCESS, r.output

    state = load_ots(sd)
    state = replace(
        state,
        phase=Phase.CONTENT_UPGRADED,
        pending_digests=(da,),
        proofs={da: verified_content_proof(da)},
    )
    save_ots(sd, state)
    return ContentUpgradedStaging(
        sd, da, hashlib.sha256(ots_src.read_bytes()).hexdigest()
    )


class TestInventoryBuild:
    def test_inventory_build_includes_ots_content_files(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, da, ots_digest = _setup_content_upgraded(
            runner, empty_sha256_root, staging, "obj:inv"
        )

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:inv",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output

        state = load_ots(sd)
        assert state.phase is Phase.INVENTORY_PENDING
        assert state.inventory is not None
        assert state.inventory_proof is not None

        plan_bytes = plan_path(sd).read_bytes()
        plan2 = CommitPlan.from_json(plan_bytes)
        manifest = plan2.inventory.inventory.manifest
        assert da in manifest, f"data digest {da!r} missing from manifest"
        assert ots_digest in manifest, (
            f".ots companion digest {ots_digest!r} missing from manifest"
        )

        assert state.inventory.digest == plan2.inventory.digest
        assert state.inventory.version == plan2.version
        assert state.inventory.job_id is None

    def test_inventory_build_uses_staged_metadata(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, _da, _ots_digest = _setup_content_upgraded(
            runner, empty_sha256_root, staging, "obj:metadata"
        )

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:metadata",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output

        plan2 = CommitPlan.from_json(plan_path(sd).read_bytes())
        vi = plan2.inventory.inventory.versions[plan2.version]
        assert vi.message == "Timestamped inventory"
        assert vi.user is not None
        assert vi.user.name == "Alice"
        assert vi.user.address == "mailto:alice@example.org"


class TestInventorySubmissionPrimitives:
    def test_prepare_persists_intent_before_submit_posts(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, _da, _ots_digest = _setup_content_upgraded(
            runner, empty_sha256_root, staging, "obj:inv-primitive"
        )
        cfg = Config(
            simpleproof_section=SimpleProofConfig(
                base_url="https://stage.example",
                proof_category_id=641,
                access_token=SecretStr("tok"),
                api_key=SecretStr("key"),
            )
        )
        store = StagingStore(staging)
        sr = StorageRoot.open(LocalFS(empty_sha256_root))

        run = _run(cfg, store, sr, empty_sha256_root, "obj:inv-primitive", sd, sp_fake)
        with with_lock(sd, run.runtime) as locked:
            prepared = wf_timestamp.prepare_submission_locked(
                run.area, locked, category_config=641, runtime=run.runtime
            )

        (digest,) = prepared.digests
        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_UPGRADED, "phase moves only after the POST"
        assert state.inventory is not None
        assert state.inventory.digest == digest
        (intent,) = [s for s in state.submissions if s.target == "inventory"]
        assert (intent.status, intent.digests) == ("submitting", (digest,))
        assert not sp_fake._submissions
        assert plan_path(sd).is_file(), "plan.json is durable before the POST"

        with with_lock(sd, run.runtime) as locked:
            result = wf_timestamp.submit_prepared_locked(run, locked, prepared)

        assert result.submitted_count == 1
        assert len(sp_fake._submissions) == 1
        assert sp_fake._submissions[0].digests == (digest,)
        state = load_ots(sd)
        assert state.phase is Phase.INVENTORY_PENDING
        (sent,) = [s for s in state.submissions if s.target == "inventory"]
        assert sent.status == "submitted"

    def test_prepare_inventory_upgraded_is_noop(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, _da, _ots_digest = _setup_content_upgraded(
            runner, empty_sha256_root, staging, "obj:inv-primitive-noop"
        )
        save_ots(sd, replace(load_ots(sd), phase=Phase.INVENTORY_UPGRADED))
        cfg = Config(
            simpleproof_section=SimpleProofConfig(
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
        with with_lock(sd, run.runtime) as locked:
            prepared = wf_timestamp.prepare_submission_locked(
                run.area, locked, category_config=641, runtime=run.runtime
            )
            result = wf_timestamp.submit_prepared_locked(run, locked, prepared)

        assert prepared.digests == ()
        assert result.submitted_count == 0
        assert load_ots(sd).phase is Phase.INVENTORY_UPGRADED
        assert not sp_fake._submissions


def _run(cfg, store, sr, root, object_id, sd, sp_fake) -> wf_timestamp.TimestampRun:
    from fakes import InMemoryHashBlockStream, RecordingSleeper

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
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, da, _ots_digest = _setup_content_upgraded(
            runner, empty_sha256_root, staging, "obj:reorg-gate"
        )
        state = load_ots(sd)
        state = replace(
            state,
            proofs={da: replace(state.proofs[da], verified=False)},
        )
        save_ots(sd, state)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:reorg-gate",
                "--expect",
                "inventory",
            ),
        )

        assert r.exit_code == ExitCode.PHASE, r.output
        assert "unverified" in r.output.lower()
        assert not plan_path(sd).exists()
        assert not sp_fake._submissions
        assert load_ots(sd).phase is Phase.CONTENT_UPGRADED

    def test_inventory_timestamp_blocked_after_reorg_reverify_resets_content(
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
            "obj:reorg-real",
            ("a.txt", b"alpha"),
            new_args=VERSION_METADATA,
        )
        d = digests["a.txt"]
        ots_bytes = final_proof_for(d)
        cache = ots_cache_path(sd, d)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(ots_bytes)
        bury(btc_fake.rpc)
        register_attested(btc_fake.rpc, d)
        state = replace(
            load_ots(sd),
            phase=Phase.CONTENT_UPGRADED,
            pending_digests=(d,),
            verify_jobs=(
                VerifyJob(job_id="old-job", digests=(d,), status="COMPLETED"),
            ),
            proofs={d: verified_content_proof(d)},
        )
        save_ots(sd, state)

        replace_attested_block(btc_fake.rpc, confs=MIN_CONFS)
        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:reorg-real",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert load_ots(sd).proofs[d].verified is False

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:reorg-real",
                "--expect",
                "inventory",
            ),
        )

        assert r.exit_code == ExitCode.PHASE, r.output
        assert "unverified" in r.output.lower()
        assert not plan_path(sd).exists()
        assert load_ots(sd).phase is Phase.CONTENT_UPGRADED


class TestMetadataGate:
    def test_missing_staged_metadata_blocks_inventory_timestamp(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)

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
            stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:missing-metadata",
                "--expect",
                "inventory",
            ),
        )

        assert r.exit_code == ExitCode.GENERIC
        assert "missing staged metadata" in r.output.lower()
        assert "message" in r.output
        assert "user.address" in r.output
        assert not plan_path(sd).exists()
        assert not sp_fake._submissions

    def test_missing_metadata_file_blocks_inventory_timestamp(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, _da, _ots_digest = _setup_content_upgraded(
            runner, empty_sha256_root, staging, "obj:missing-metadata-file"
        )
        (sd / "metadata.json").unlink()

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:missing-metadata-file",
                "--expect",
                "inventory",
            ),
        )

        assert r.exit_code == ExitCode.GENERIC
        assert "missing staged metadata" in r.output.lower()
        assert "message" in r.output
        assert "user.name" in r.output
        assert "user.address" in r.output
        assert not plan_path(sd).exists()
        assert not sp_fake._submissions

    def test_allow_missing_metadata_bypasses_inventory_timestamp_gate(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)

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
            stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:allow-metadata",
                "--allow-missing-metadata",
                "--expect",
                "inventory",
            ),
        )

        assert r.exit_code == ExitCode.SUCCESS, r.output
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
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        verb_args: tuple[str, ...],
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        _setup_content_upgraded(
            runner, empty_sha256_root, staging, "obj:irrelevant-flag"
        )

        verb, *flags = verb_args
        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                verb,
                "obj:irrelevant-flag",
                *flags,
                "--allow-missing-metadata",
            ),
        )

        assert r.exit_code == ExitCode.USAGE
        assert "--allow-missing-metadata" in r.output


class TestPlanRoundTrip:
    def test_plan_json_round_trip_has_no_inventory_proof_yet(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, _da, _ots_digest = _setup_content_upgraded(
            runner, empty_sha256_root, staging, "obj:rt"
        )

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:rt",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output

        raw = plan_path(sd).read_bytes()
        plan2 = CommitPlan.from_json(raw)

        state = load_ots(sd)
        assert state.inventory is not None
        assert plan2.inventory.digest == state.inventory.digest
        assert plan2.inventory_proof is None


class TestPhaseGate:
    def test_phase_error_at_drafting_with_hint(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:err-draft",
            ("a.txt", b"alpha"),
            new_args=VERSION_METADATA,
        )

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:err-draft",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == ExitCode.PHASE, r.output
        assert r.output == (
            "Error: command 'timestamp inventory' not legal in phase 'drafting'; "
            "next: ocfl stage timestamp obj:err-draft\n"
        )

    def test_phase_error_at_content_pending_with_hint(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, _digests = new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:err-pend",
            ("a.txt", b"alpha"),
            new_args=VERSION_METADATA,
        )
        save_ots(sd, replace(load_ots(sd), phase=Phase.CONTENT_PENDING))

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:err-pend",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == ExitCode.PHASE, r.output
        assert r.output == (
            "Error: command 'timestamp inventory' not legal in phase "
            "'content-pending'; next: ocfl stage upgrade obj:err-pend --watch\n"
        )


class TestResubmitAtInventoryPending:
    def test_rerun_at_inventory_pending_sends_nothing(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The submitted digest keeps its requested proof (ADR-0004), so a
        second run at `inventory-pending` does not POST it again."""
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, _da, _ots_digest = _setup_content_upgraded(
            runner, empty_sha256_root, staging, "obj:resubmit"
        )

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:resubmit",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        s1 = load_ots(sd)
        assert s1.phase is Phase.INVENTORY_PENDING
        assert s1.inventory is not None
        assert len(sp_fake._submissions) == 1
        first_inv_digest = s1.inventory.digest
        assert sp_fake._submissions[0].digests == (first_inv_digest,)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:resubmit",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert load_ots(sd).phase is Phase.INVENTORY_PENDING
        assert len(sp_fake._submissions) == 1
        assert load_ots(sd).inventory == s1.inventory


class TestInventoryCrashAtPost:
    def test_plan_names_the_intent_and_next_run_resends_it(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)
        staging = tmp_path / "staging"
        sd, _da, _ots_digest = _setup_content_upgraded(
            runner, empty_sha256_root, staging, "obj:inv-crash"
        )
        timestamp = stage_args(
            empty_sha256_root,
            staging,
            "timestamp",
            "obj:inv-crash",
            "--expect",
            "inventory",
        )

        with CrashBoundary(sp_fake, "submit_hashes"), pytest.raises(Crash):
            runner.invoke(app, timestamp)

        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_UPGRADED
        assert state.inventory is not None
        digest = state.inventory.digest
        (intent,) = [s for s in state.submissions if s.target == "inventory"]
        assert (intent.status, intent.digests) == ("submitting", (digest,))
        plan_bytes = plan_path(sd).read_bytes()
        assert CommitPlan.from_json(plan_bytes).inventory.digest == digest

        r = runner.invoke(app, timestamp)
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert [s.digests for s in sp_fake._submissions] == [(digest,)]
        state = load_ots(sd)
        assert state.phase is Phase.INVENTORY_PENDING
        assert state.inventory is not None
        assert state.inventory.digest == digest
        assert plan_path(sd).read_bytes() == plan_bytes


class TestNoopAtInventoryUpgraded:
    def test_noop_at_inventory_upgraded(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, _da, _ots_digest = _setup_content_upgraded(
            runner, empty_sha256_root, staging, "obj:noop"
        )

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:noop",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert load_ots(sd).phase is Phase.INVENTORY_PENDING
        save_ots(sd, replace(load_ots(sd), phase=Phase.INVENTORY_UPGRADED))

        pre_calls = len(sp_fake._submissions)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:noop",
                "--expect",
                "inventory",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert load_ots(sd).phase is Phase.INVENTORY_UPGRADED
        assert len(sp_fake._submissions) == pre_calls
