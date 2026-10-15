import hashlib
import os
from pathlib import Path

import pytest
from fakes import FakeSimpleProof
from msgspec.structs import replace
from ots_helpers import (
    DIGEST_BLOCK_HEIGHT,
    MIN_CONFS,
    SP_OK,
    bury,
    crash_at,
    end_chain_below,
    final_proof_for,
    inject_fake_btc,
    inject_fake_sp,
    new_ots_staging,
    register_attested,
    register_mismatched_merkle_root,
    replace_attested_block,
    write_config,
)
from runner_helpers import ObjRunner, stage_args

from ocfl_cli._style import make_console
from ocfl_cli.errors import AttestationMismatchError
from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app
from ocfl_cli.model.ots_state import (
    CalendarAttestation,
    Proof,
    Submission,
    VerifyJob,
)
from ocfl_cli.model.phase import Phase, TimestampTarget
from ocfl_cli.model.simpleproof import CalendarAttestation as WireCalendarAttestation
from ocfl_cli.stage import timestamp as stage_timestamp
from ocfl_cli.store.ots_state import load_ots, ots_cache_path, save_ots
from ocfl_cli.store.staging import StagingStore
from ocfl_cli.workflow import common as wf_common
from ocfl_cli.workflow import timestamp as wf_timestamp
from tests.helpers.faults import Crash, CrashBoundary
from tests.helpers.ots import build_attestation


def _ots_pending(digest_hex: str) -> bytes:
    return build_attestation(
        bytes.fromhex(digest_hex),
        pending_uri="https://a.pool.opentimestamps.org",
    )


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


def _seed_processing_verify_job(
    sp_fake: FakeSimpleProof,
    sd: Path,
    digests: tuple[str, ...],
    results: dict[str, tuple[WireCalendarAttestation, ...]],
    *,
    updated_at: str | None = "2026-05-15T12:03:00Z",
) -> str:
    job_id = sp_fake.verify_batch(list(digests)).job_id
    sp_fake.advance_to_processing(job_id, results, updated_at=updated_at)
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


class TestProgressRendering:
    def test_one_shot_content_result_renders_jobs_and_warning(self, capsys):
        result = wf_timestamp.UpgradeResult(
            target=TimestampTarget.CONTENT,
            previous_phase=Phase.CONTENT_PENDING,
            phase=Phase.CONTENT_PENDING,
            unverified_pending_count=63,
            jobs=(
                wf_common.JobProgress(
                    job_id="ab12cd34-full",
                    status="PROCESSING",
                    total_hashes=250,
                    verified_hashes=187,
                    age_seconds=7200,
                    anchored_digests=187,
                    pending_digests=63,
                    calendar_counts=(
                        ("bob.btc.calendar.opentimestamps.org", 187),
                        ("alice.btc.calendar.opentimestamps.org", 184),
                    ),
                    submitted_hashes=250,
                    total_documents_found=249,
                ),
            ),
        )

        stage_timestamp.render_upgrade_result(make_console(), "obj:render", result)

        out = capsys.readouterr().out
        assert "Content:" in out
        assert "  Object: obj:render" in out
        assert "  Warning: SP recognized 249 of 250 submitted hashes" in out
        assert "  Jobs:" in out
        assert "    job ab12cd34 PROCESSING — 187/250 verified (75%), age 2h ago" in out
        assert "      anchored: 187 digests" in out
        assert "        bob.btc.calendar.opentimestamps.org: 187" in out
        assert "        alice.btc.calendar.opentimestamps.org: 184" in out
        assert "      pending: 63 digests" in out
        assert "  Waiting on 63 pending digests" in out

    def test_watch_progress_renders_deltas_and_no_change(self, capsys):
        prior = (
            wf_common.JobProgress(
                job_id="ab12cd34-full",
                status="PROCESSING",
                total_hashes=250,
                verified_hashes=187,
                age_seconds=None,
                anchored_digests=187,
                pending_digests=63,
                calendar_counts=(("bob.btc.calendar.opentimestamps.org", 187),),
            ),
            wf_common.JobProgress(
                job_id="ef56gh78-full",
                status="PROCESSING",
                total_hashes=250,
                verified_hashes=100,
                age_seconds=None,
                anchored_digests=100,
                pending_digests=150,
                calendar_counts=(("bob.btc.calendar.opentimestamps.org", 100),),
            ),
        )
        current = (
            wf_common.JobProgress(
                job_id="ab12cd34-full",
                status="PROCESSING",
                total_hashes=250,
                verified_hashes=200,
                age_seconds=None,
                anchored_digests=193,
                pending_digests=57,
                calendar_counts=(("bob.btc.calendar.opentimestamps.org", 193),),
            ),
            wf_common.JobProgress(
                job_id="ef56gh78-full",
                status="PROCESSING",
                total_hashes=250,
                verified_hashes=100,
                age_seconds=None,
                anchored_digests=100,
                pending_digests=150,
                calendar_counts=(("bob.btc.calendar.opentimestamps.org", 100),),
            ),
        )

        stage_timestamp.render_watch_progress(
            make_console(),
            "block",
            {
                "height": 920150,
                "phase": "content-pending",
                "reverify": False,
                "jobs": current,
                "prior_jobs": prior,
            },
            next_action=lambda: "ocfl stage timestamp obj:render",
        )

        out = capsys.readouterr().out
        assert "Block:" in out
        assert "  Height: 920150" in out
        assert (
            "    job ab12cd34 PROCESSING — 200/250 verified (+13 since last block)"
            in out
        )
        assert "      anchored: +6 digests" in out
        assert "        bob.btc.calendar.opentimestamps.org: 193 (+6)" in out
        assert "    job ef56gh78 PROCESSING — 100/250 verified (no change)" in out
        assert "      anchored: 100 digests" in out
        assert "      pending: 150 digests" in out

    def test_watch_start_renders_submit_mismatch_warning_once(self, capsys):
        stage_timestamp.render_watch_start(
            make_console(),
            "obj:watch",
            (
                wf_common.JobProgress(
                    job_id="job-warning",
                    status="PROCESSING",
                    total_hashes=10,
                    verified_hashes=0,
                    age_seconds=None,
                    anchored_digests=0,
                    pending_digests=10,
                    calendar_counts=(),
                    submitted_hashes=10,
                    total_documents_found=8,
                ),
            ),
        )

        out = capsys.readouterr().out
        assert "Watch:" in out
        assert "  Object: obj:watch" in out
        assert "  Warning: SP recognized 8 of 10 submitted hashes" in out
        assert out.count("Warning: SP recognized") == 1


class TestMidflightProgressSnapshots:
    def test_snapshot_persists_attestations_on_verify_job_only(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        sp_fake: FakeSimpleProof,
        runtime,
    ):
        write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, digests_by_name = new_ots_staging(
            runner, empty_sha256_root, staging, "obj:progress", ("a.txt", b"a")
        )
        digest = digests_by_name["a.txt"]
        _force_content_pending(sd, (digest,))
        _seed_processing_verify_job(
            sp_fake,
            sd,
            (digest,),
            {
                digest: (
                    WireCalendarAttestation(
                        calendar="bob.btc.calendar.opentimestamps.org",
                        block_height=920150,
                        transaction="txid",
                        merkle_root="root",
                    ),
                )
            },
        )

        state = wf_common.poll_outstanding_jobs(load_ots(sd), sp_fake)
        state = wf_common.snapshot_results(state, sd, sp_fake)

        job = state.verify_jobs[0]
        assert digest in job.attestation_snapshots
        assert state.proofs == {}
        progress = wf_common.compute_job_progress(state, runtime=runtime)
        assert progress[0].anchored_digests == 1
        assert progress[0].pending_digests == 0
        assert progress[0].calendar_counts == (
            ("bob.btc.calendar.opentimestamps.org", 1),
        )

    def test_snapshot_rejects_foreign_digest(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        sp_fake: FakeSimpleProof,
    ):
        write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, digests_by_name = new_ots_staging(
            runner, empty_sha256_root, staging, "obj:foreign", ("a.txt", b"a")
        )
        digest = digests_by_name["a.txt"]
        foreign = hashlib.sha256(b"foreign").hexdigest()
        _force_content_pending(sd, (digest,))
        _seed_processing_verify_job(
            sp_fake,
            sd,
            (digest,),
            {foreign: ()},
        )
        state = wf_common.poll_outstanding_jobs(load_ots(sd), sp_fake)

        with pytest.raises(AttestationMismatchError, match="unexpected digest from SP"):
            wf_common.snapshot_results(state, sd, sp_fake)

    def test_unconfirmed_result_does_not_count_as_anchored(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        sp_fake: FakeSimpleProof,
        runtime,
    ):
        write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, digests_by_name = new_ots_staging(
            runner, empty_sha256_root, staging, "obj:unconfirmed", ("a.txt", b"a")
        )
        digest = digests_by_name["a.txt"]
        _force_content_pending(sd, (digest,))
        _seed_processing_verify_job(sp_fake, sd, (digest,), {digest: ()})
        state = wf_common.poll_outstanding_jobs(load_ots(sd), sp_fake)

        state = wf_common.snapshot_results(state, sd, sp_fake)

        assert state.verify_jobs[0].attestation_snapshots == {}
        progress = wf_common.compute_job_progress(state, runtime=runtime)
        assert progress[0].anchored_digests == 0
        assert progress[0].pending_digests == 1

    def test_updated_at_none_refetches_snapshot_each_pass(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        sp_fake: FakeSimpleProof,
    ):
        write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, digests_by_name = new_ots_staging(
            runner, empty_sha256_root, staging, "obj:refetch", ("a.txt", b"a")
        )
        digest = digests_by_name["a.txt"]
        _force_content_pending(sd, (digest,))
        _seed_processing_verify_job(
            sp_fake,
            sd,
            (digest,),
            {
                digest: (
                    WireCalendarAttestation(
                        calendar="bob.btc.calendar.opentimestamps.org",
                        block_height=920150,
                        transaction="txid",
                        merkle_root="root",
                    ),
                )
            },
            updated_at=None,
        )
        calls = 0
        original = sp_fake.fetch_results_entries

        def counted_fetch(results_url: str):
            nonlocal calls
            calls += 1
            return original(results_url)

        monkeypatch.setattr(sp_fake, "fetch_results_entries", counted_fetch)
        state = wf_common.poll_outstanding_jobs(load_ots(sd), sp_fake)

        state = wf_common.snapshot_results(state, sd, sp_fake)
        state = wf_common.snapshot_results(state, sd, sp_fake)

        assert calls == 2

    def test_snapshot_rebuilds_attestation_dict_identity(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        sp_fake: FakeSimpleProof,
    ):
        write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, digests_by_name = new_ots_staging(
            runner, empty_sha256_root, staging, "obj:identity", ("a.txt", b"a")
        )
        digest = digests_by_name["a.txt"]
        _force_content_pending(sd, (digest,))
        _seed_processing_verify_job(
            sp_fake,
            sd,
            (digest,),
            {
                digest: (
                    WireCalendarAttestation(
                        calendar="bob.btc.calendar.opentimestamps.org",
                        block_height=920150,
                        transaction="txid",
                        merkle_root="root",
                    ),
                )
            },
        )
        state = wf_common.poll_outstanding_jobs(load_ots(sd), sp_fake)
        before = state.verify_jobs[0].attestation_snapshots

        state = wf_common.snapshot_results(state, sd, sp_fake)

        after = state.verify_jobs[0].attestation_snapshots
        assert after is not before
        assert digest in after

    def test_resubmitted_job_starts_with_empty_snapshots(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        sp_fake: FakeSimpleProof,
        btc_fake,
    ):
        write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, digests_by_name = new_ots_staging(
            runner, empty_sha256_root, staging, "obj:resubmit", ("a.txt", b"a")
        )
        digest = digests_by_name["a.txt"]
        cache_file = ots_cache_path(sd, digest)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(final_proof_for(digest))
        bury(btc_fake.rpc)
        register_mismatched_merkle_root(btc_fake.rpc)
        state = load_ots(sd)
        state = replace(
            state,
            phase=Phase.CONTENT_PENDING,
            pending_digests=(digest,),
            verify_jobs=(
                VerifyJob(
                    job_id="old-job",
                    digests=(digest,),
                    status="COMPLETED",
                    attestation_snapshots={
                        digest: (
                            CalendarAttestation(
                                calendar="bob.btc.calendar.opentimestamps.org",
                                block_height=920150,
                                transaction="txid",
                                merkle_root="root",
                            ),
                        )
                    },
                ),
            ),
            proofs={
                digest: Proof(
                    target=TimestampTarget.CONTENT,
                    ots_path=f"ots-cache/{digest}.ots",
                    verified=True,
                )
            },
        )

        state = wf_timestamp.reverify(
            state,
            (digest,),
            btc=btc_fake.rpc,
            sp=sp_fake,
            staging_dir=sd,
            min_confirmations=MIN_CONFS,
            category_id=641,
        )

        assert len(state.verify_jobs) == 1
        assert state.verify_jobs[0].job_id != "old-job"
        assert state.verify_jobs[0].attestation_snapshots == {}

    def test_verified_ingest_preserves_attestation_snapshots(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        sp_fake: FakeSimpleProof,
        btc_fake,
        runtime,
    ):
        write_config(tmp_path, monkeypatch)
        inject_fake_btc(runner, btc_fake.rpc)
        staging = tmp_path / "staging"
        sd, digests_by_name = new_ots_staging(
            runner, empty_sha256_root, staging, "obj:preserve", ("a.txt", b"a")
        )
        digest = digests_by_name["a.txt"]
        _force_content_pending(sd, (digest,))
        ots_bytes = final_proof_for(digest)
        job_id = sp_fake.verify_batch([digest]).job_id
        sp_fake.advance_to_completed(job_id, {digest: ots_bytes})
        snapshot: dict[str, tuple[CalendarAttestation, ...]] = {
            digest: (
                CalendarAttestation(
                    calendar="bob.btc.calendar.opentimestamps.org",
                    block_height=920150,
                    transaction="txid",
                    merkle_root="root",
                ),
            )
        }
        state = replace(
            load_ots(sd),
            verify_jobs=(
                VerifyJob(
                    job_id=job_id,
                    digests=(digest,),
                    status="COMPLETED",
                    results_url=f"results://{job_id}",
                    attestation_snapshots=snapshot,
                ),
            ),
        )
        save_ots(sd, state)
        bury(btc_fake.rpc)
        register_attested(btc_fake.rpc, digest)

        downloads = sp_fake.download_results(f"results://{job_id}", {digest})
        state = wf_timestamp.verify_digest(
            digest,
            downloads[digest],
            state,
            TimestampTarget.CONTENT,
            btc=btc_fake.rpc,
            staging_dir=sd,
            min_confirmations=MIN_CONFS,
            runtime=runtime,
        )

        assert state.proofs[digest].verified is True
        assert state.verify_jobs[0].attestation_snapshots == snapshot


class TestUpgradeHappyPath:
    def test_single_digest_at_3_confs_advances_to_content_upgraded(
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
            runner, empty_sha256_root, staging, "obj:up", ("a.txt", b"alpha")
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
                "obj:up",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert "Content:" in r.output
        assert "Object: obj:up" in r.output
        assert "Checked SimpleProof jobs" in r.output
        assert "Workflow:" in r.output
        assert "Phase: content-pending -> content-upgraded" in r.output
        assert "Next: ocfl stage timestamp obj:up" in r.output

        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_UPGRADED
        proof = state.proofs[d]
        assert proof.verified is True
        assert proof.block_height == DIGEST_BLOCK_HEIGHT
        assert proof.block_hash == btc_fake.rpc.getblockhash(DIGEST_BLOCK_HEIGHT)
        assert proof.confirmations_at_verify is not None
        assert proof.confirmations_at_verify >= MIN_CONFS
        assert proof.verified_at is not None
        assert proof.ots_path == f"ots-cache/{d}.ots"

        assert ots_cache_path(sd, d).read_bytes() == ots_bytes

        store = StagingStore(staging)
        root_path = str(empty_sha256_root.resolve())
        inv = store.load_inventory(root_path, "obj:up")
        all_paths = {p for ps in inv.versions[inv.head].state.values() for p in ps}
        assert "a.txt" in all_paths
        assert "a.txt.ots" in all_paths
        cm = store.load_content_map(root_path, "obj:up")
        ots_digest = hashlib.sha256(ots_bytes).hexdigest()
        assert ots_digest in cm

    def test_multi_digest_partial_pending_does_not_advance(
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
            "obj:multi",
            ("a.txt", b"alpha"),
            ("b.txt", b"bravo"),
        )
        da, db = digests["a.txt"], digests["b.txt"]
        _force_content_pending(sd, (da, db))

        ots_a = final_proof_for(da)
        ots_b = _ots_pending(db)
        _seed_completed_verify_job(sp_fake, sd, (da, db), {da: ots_a, db: ots_b})

        bury(btc_fake.rpc)
        register_attested(btc_fake.rpc, da)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:multi",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.PENDING, r.output
        assert "Content:" in r.output
        assert "Object: obj:multi" in r.output
        assert "Checked SimpleProof jobs" in r.output
        assert "Waiting on 1 pending digest" in r.output
        assert "Workflow:" in r.output
        assert "Phase: content-pending" in r.output
        assert "Next: ocfl stage upgrade obj:multi --watch" in r.output

        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_PENDING
        assert state.proofs[da].verified is True
        assert state.proofs[db].verified is False
        assert ots_cache_path(sd, da).is_file()
        assert ots_cache_path(sd, db).is_file()


class TestThreeConfGate:
    @pytest.mark.parametrize(
        ("confs", "expect_advance"),
        [
            (MIN_CONFS - 2, False),
            (MIN_CONFS - 1, False),
            (MIN_CONFS, True),
            (MIN_CONFS + 1, True),
        ],
    )
    def test_advance_only_at_or_above_min_confirmations(
        self,
        runner: ObjRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        confs: int,
        expect_advance: bool,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)
        inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, digests = new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:gate",
            ("a.txt", b"alpha"),
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes = final_proof_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})

        bury(btc_fake.rpc, confs)
        register_attested(btc_fake.rpc, d)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:gate",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == (
            ExitCode.SUCCESS if expect_advance else ExitCode.PENDING
        ), r.output

        state = load_ots(sd)
        if expect_advance:
            assert state.phase is Phase.CONTENT_UPGRADED
            assert state.proofs[d].verified is True
        else:
            assert state.phase is Phase.CONTENT_PENDING
            assert ots_cache_path(sd, d).is_file()
            assert state.proofs[d].verified is False


class TestPendingHeld:
    def test_pending_attestation_held_then_resolves_on_next_pass(
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
            "obj:pend",
            ("a.txt", b"alpha"),
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        pending_bytes = _ots_pending(d)
        job_id = _seed_completed_verify_job(sp_fake, sd, (d,), {d: pending_bytes})
        bury(btc_fake.rpc)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:pend",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.PENDING, r.output
        assert "Waiting on 1 pending digest" in r.output
        assert "Phase: content-pending" in r.output
        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_PENDING
        assert state.proofs[d].verified is False
        assert ots_cache_path(sd, d).is_file()

        anchored = final_proof_for(d)
        sp_fake._results[f"results://{job_id}"] = {d: anchored}
        register_attested(btc_fake.rpc, d)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:pend",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert "Phase: content-pending -> content-upgraded" in r.output
        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_UPGRADED
        assert state.proofs[d].verified is True


class TestReorgShallow:
    def test_reorg_of_attested_block_within_n_marks_unverified(
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
            "obj:reorg-s",
            ("a.txt", b"alpha"),
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
                "obj:reorg-s",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert load_ots(sd).proofs[d].verified is True

        replace_attested_block(btc_fake.rpc, confs=1)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:reorg-s",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert "Waiting on 1 pending digest" in r.output
        state = load_ots(sd)
        assert state.proofs[d].verified is False


class TestReorgDeep:
    def test_reorg_deeper_than_n_halts_with_exit_5(
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
            "obj:reorg-d",
            ("a.txt", b"alpha"),
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes = final_proof_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})

        end_chain_below(btc_fake.rpc, MIN_CONFS + 5)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:reorg-d",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.ATTESTATION, r.output
        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_PENDING


class TestMerkleMismatch:
    def test_wrong_merkle_root_halts_with_exit_5(
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
            "obj:mm",
            ("a.txt", b"alpha"),
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes = final_proof_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})

        bury(btc_fake.rpc)
        register_mismatched_merkle_root(btc_fake.rpc)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:mm",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.ATTESTATION, r.output
        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_PENDING
        assert d not in state.proofs or not state.proofs[d].verified


class TestReorgMerkleMismatch:
    def test_reorg_replaces_block_at_sufficient_confs_resets_and_resubmits(
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
            "obj:reorg-mm",
            ("a.txt", b"alpha"),
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
                "obj:reorg-mm",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert load_ots(sd).proofs[d].verified is True

        replace_attested_block(btc_fake.rpc, confs=MIN_CONFS)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:reorg-mm",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        state = load_ots(sd)
        assert state.proofs[d].verified is False
        assert any(
            d in j.digests and j.status == "IN_PROGRESS" for j in state.verify_jobs
        )


class TestContentUpgradeExpired:
    def test_expired_content_verify_job_triggers_fresh_batch(
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
            "obj:content-exp",
            ("a.txt", b"alpha"),
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        dead_id = sp_fake.verify_batch([d]).job_id
        sp_fake.expire_job(dead_id)
        state = replace(
            load_ots(sd),
            verify_jobs=(VerifyJob(job_id=dead_id, digests=(d,), status="EXPIRED"),),
        )
        save_ots(sd, state)
        assert len(sp_fake._jobs) == 1

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:content-exp",
                "--expect",
                "content",
            ),
        )

        assert r.exit_code == ExitCode.PENDING, r.output
        assert len(sp_fake._jobs) == 2
        state = load_ots(sd)
        assert any(
            d in j.digests and j.status not in {"FAILED", "EXPIRED", "COMPLETED"}
            for j in state.verify_jobs
        )


class TestConfirmationsBeforeMerkleCli:
    def test_shallow_wrong_merkle_waits_but_trusted_depth_halts(
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
            "obj:shallow-mm",
            ("a.txt", b"alpha"),
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes = final_proof_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})

        bury(btc_fake.rpc, 1)
        register_mismatched_merkle_root(btc_fake.rpc)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:shallow-mm",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.PENDING, r.output
        assert "Waiting on 1 pending digest" in r.output
        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_PENDING
        assert state.proofs[d].verified is False

        bury(btc_fake.rpc)
        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:shallow-mm",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.ATTESTATION, r.output
        assert "halt:" in r.output


class TestCrashRecovery:
    @pytest.mark.parametrize(
        ("boundary", "cached", "verified"),
        [
            ("cache", False, False),
            ("proof", True, False),
            ("proof_torn", True, False),
            ("companion", True, True),
        ],
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
        verified: bool,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)
        inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, digests = new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:crash",
            ("a.txt", b"alpha"),
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes = final_proof_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})
        bury(btc_fake.rpc)
        register_attested(btc_fake.rpc, d)
        upgrade = stage_args(
            empty_sha256_root, staging, "upgrade", "obj:crash", "--expect", "content"
        )
        store = StagingStore(staging)

        def staged_paths() -> set[str]:
            inv = store.load_inventory(str(empty_sha256_root.resolve()), "obj:crash")
            return {p for ps in inv.versions[inv.head].state.values() for p in ps}

        with (
            CrashBoundary(os, "replace", crash_at(boundary, sd, d)),
            pytest.raises(Crash),
        ):
            runner.invoke(app, upgrade)

        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_PENDING
        assert (ots_cache_path(sd, d).is_file(), state.verified(d)) == (
            cached,
            verified,
        )
        assert "a.txt.ots" not in staged_paths()

        r = runner.invoke(app, upgrade)
        assert r.exit_code == ExitCode.SUCCESS, r.output
        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_UPGRADED
        assert state.verified(d)
        assert ots_cache_path(sd, d).read_bytes() == ots_bytes
        assert "a.txt.ots" in staged_paths()


class TestOrphanGuard:
    def test_mid_flight_stage_add_blocks_phase_advance(
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
            "obj:orphan",
            ("a.txt", b"alpha"),
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        ots_bytes = final_proof_for(d)
        _seed_completed_verify_job(sp_fake, sd, (d,), {d: ots_bytes})
        bury(btc_fake.rpc)
        register_attested(btc_fake.rpc, d)

        new_src = tmp_path / "b.txt"
        new_src.write_bytes(b"bravo")
        r = runner.invoke(
            app,
            stage_args(empty_sha256_root, staging, "add", "obj:orphan", str(new_src)),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output

        state = load_ots(sd)
        if state.phase is not Phase.CONTENT_PENDING:
            save_ots(sd, replace(state, phase=Phase.CONTENT_PENDING))

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:orphan",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.PENDING, r.output
        assert "Held by 1 orphan digest" in r.output
        assert "Phase: content-pending" in r.output
        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_PENDING, "an orphan digest holds the phase"
        assert state.proofs[d].verified is True
        d_b = hashlib.sha256(b"bravo").hexdigest()
        assert d_b not in state.pending_digests


class TestPhaseAdvanceInvariant:
    def test_advance_when_all_pending_verified_and_no_unpending_content(
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
            "obj:inv-ok",
            ("a.txt", b"alpha"),
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
                "obj:inv-ok",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert load_ots(sd).phase is Phase.CONTENT_UPGRADED

    def test_no_advance_while_a_pending_digest_is_unverified(
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
            "obj:inv-1",
            ("a.txt", b"alpha"),
        )
        d = digests["a.txt"]
        _force_content_pending(sd, (d,))

        _seed_completed_verify_job(sp_fake, sd, (d,), {d: final_proof_for(d)})
        bury(btc_fake.rpc, 1)
        register_attested(btc_fake.rpc, d)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:inv-1",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.PENDING, r.output
        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_PENDING
        assert state.proofs[d].verified is False

    def test_no_advance_while_staged_content_digest_is_not_pending(
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
            "obj:inv-2",
            ("a.txt", b"alpha"),
            ("b.txt", b"bravo"),
        )
        da = digests["a.txt"]
        _force_content_pending(sd, (da,))

        ots_a = final_proof_for(da)
        _seed_completed_verify_job(sp_fake, sd, (da,), {da: ots_a})
        bury(btc_fake.rpc)
        register_attested(btc_fake.rpc, da)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:inv-2",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.PENDING, r.output
        state = load_ots(sd)
        assert state.proofs[da].verified is True
        assert state.phase is Phase.CONTENT_PENDING


class TestReverifyIdempotent:
    def test_second_upgrade_pass_with_no_changes_is_noop(
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
            "obj:idem",
            ("a.txt", b"alpha"),
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
                "obj:idem",
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
                "obj:idem",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert load_ots(sd).phase is Phase.CONTENT_UPGRADED


@pytest.mark.bitcoin
class TestRegtestEndToEnd:
    def test_real_rpc_real_attestation_with_invalidateblock_reorg(
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

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:rt",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_UPGRADED
        assert state.proofs[d].verified is True
        assert state.proofs[d].block_height == attest_height

        bitcoind_regtest.rpc("invalidateblock", [attest_hash])
        addr = bitcoind_regtest.wallet_rpc("ocfltest", "getnewaddress")
        bitcoind_regtest.rpc("generatetoaddress", [MIN_CONFS + 2, addr])

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:rt",
                "--expect",
                "content",
            ),
        )
        post = load_ots(sd)
        assert (
            post.proofs[d].verified is False or post.phase is not Phase.CONTENT_UPGRADED
        ), (
            f"reorg should have invalidated the proof; phase={post.phase} "
            f"verified={post.proofs[d].verified} exit={r.exit_code}"
        )
