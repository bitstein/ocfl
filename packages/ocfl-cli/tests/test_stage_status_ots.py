import json
from pathlib import Path

import pytest
from fakes import FakeClock, FakeSimpleProof
from fakes.locks import held_flock
from msgspec.structs import replace
from ots_helpers import (
    CATEGORY_ID,
    SP_OK,
    inject_fake_btc,
    inject_fake_sp,
    new_ots_staging,
    verified_content_proof,
    write_config,
)
from runner_helpers import runner_obj, stage_args, staging_dir
from typer.testing import CliRunner

from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app
from ocfl_cli.model.ots_state import (
    Proof,
    StagedInventory,
    Submission,
    VerifyJob,
)
from ocfl_cli.model.phase import Phase, TimestampTarget
from ocfl_cli.store.ots_state import load_ots, save_ots


def _stage_args_json(root: Path, staging: Path, *args: str) -> list[str]:
    return [
        "--root",
        str(root),
        "--json",
        "stage",
        "--staging-root",
        str(staging),
        *args,
    ]


# --------------------------------------------------------------------------- #
# Setup helpers: bring an OTS staging into each phase by mutating ots.json
# directly. The status command READS ots.json without enforcing the FSM
# (status itself is `_STAY` in the transition matrix), so direct mutation
# is sufficient and avoids dragging in unrelated workflow scaffolding.
# --------------------------------------------------------------------------- #


def _set_phase(
    sd: Path,
    phase: Phase,
    *,
    pending_digests: tuple[str, ...] = (),
    submissions: tuple[Submission, ...] = (),
    verify_jobs: tuple[VerifyJob, ...] = (),
    proofs: dict[str, Proof] | None = None,
    inventory: StagedInventory | None = None,
) -> None:
    state = load_ots(sd)
    state = replace(
        state,
        phase=phase,
        pending_digests=pending_digests,
        submissions=submissions,
        verify_jobs=verify_jobs,
        proofs=proofs if proofs is not None else {},
        inventory=inventory,
    )
    save_ots(sd, state)


def _verified_inv_proof(version: str, inv_digest: str) -> StagedInventory:
    return StagedInventory(version=version, digest=inv_digest, job_id=None)


class TestStageStatusOts:
    def test_status_drafting_no_files_next_action_add_content(
        self,
        runner: CliRunner,
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
        new_ots_staging(runner, empty_sha256_root, staging, "obj:s-empty")

        r = runner.invoke(
            app, _stage_args_json(empty_sha256_root, staging, "status", "obj:s-empty")
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        data = json.loads(r.output)
        assert data["phase"] == Phase.DRAFTING.value
        assert data["next_action_kind"] == "add_content"
        assert data["counts"]["total"] == 0
        assert data["metadata"] == {
            "message": None,
            "user.name": None,
            "user.address": None,
        }

    def test_status_ots_shows_staged_metadata_state(
        self,
        runner: CliRunner,
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
        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "new",
                "obj:s-metadata",
                "--ots",
                "--name",
                "Alice",
                "--address",
                "mailto:alice@example.org",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output

        r = runner.invoke(
            app, stage_args(empty_sha256_root, staging, "status", "obj:s-metadata")
        )

        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert "Metadata:" in r.output
        assert "message: missing" in r.output
        assert "user.name: Alice" in r.output
        assert "user.address: mailto:alice@example.org" in r.output

    def test_status_ots_json_shows_partial_staged_metadata_state(
        self,
        runner: CliRunner,
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
        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "new",
                "obj:s-metadata-json",
                "--ots",
                "--name",
                "Alice",
                "--address",
                "mailto:alice@example.org",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output

        r = runner.invoke(
            app,
            _stage_args_json(
                empty_sha256_root, staging, "status", "obj:s-metadata-json"
            ),
        )

        assert r.exit_code == ExitCode.SUCCESS, r.output
        data = json.loads(r.output)
        assert data["metadata"] == {
            "message": None,
            "user.name": "Alice",
            "user.address": "mailto:alice@example.org",
        }

    def test_status_drafting_with_files_next_action_timestamp_content(
        self,
        runner: CliRunner,
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
        new_ots_staging(
            runner, empty_sha256_root, staging, "obj:s-draft", ("a.txt", b"alpha")
        )

        r = runner.invoke(
            app, _stage_args_json(empty_sha256_root, staging, "status", "obj:s-draft")
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        data = json.loads(r.output)
        assert data["phase"] == Phase.DRAFTING.value
        assert data["next_action_kind"] == "timestamp_content"
        assert data["counts"]["total"] == 1

    def test_status_content_pending_outstanding_jobs_listed(
        self,
        runner: CliRunner,
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
            "obj:s-pend",
            ("a.txt", b"alpha"),
        )
        da = digests["a.txt"]
        _set_phase(
            sd,
            Phase.CONTENT_PENDING,
            pending_digests=(da,),
            submissions=(
                Submission(
                    submitted_at="2026-04-25T14:00:00+00:00",
                    digests=(da,),
                    status="submitted",
                    target=TimestampTarget.CONTENT,
                    attempts=1,
                ),
            ),
            verify_jobs=(
                VerifyJob(job_id="job-1", digests=(da,), status="IN_PROGRESS"),
            ),
        )

        r = runner.invoke(
            app, _stage_args_json(empty_sha256_root, staging, "status", "obj:s-pend")
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        data = json.loads(r.output)
        assert data["phase"] == Phase.CONTENT_PENDING.value
        assert data["next_action_kind"] == "upgrade_content"
        outstanding = data["outstanding_jobs"]
        assert isinstance(outstanding, list)
        assert len(outstanding) == 1
        entry = outstanding[0]
        assert entry["job_id"] == "job-1"
        assert entry["digests"] == [da]
        assert entry["status"] == "IN_PROGRESS"
        assert entry["submitted_at"] == "2026-04-25T14:00:00+00:00"
        assert isinstance(entry["age_seconds"], int)
        assert entry["age_seconds"] > 0

    def test_status_content_pending_with_failed_job_next_retry(
        self,
        runner: CliRunner,
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
            "obj:s-pend-fail",
            ("a.txt", b"alpha"),
        )
        da = digests["a.txt"]
        _set_phase(
            sd,
            Phase.CONTENT_PENDING,
            pending_digests=(da,),
            submissions=(
                Submission(
                    submitted_at="2026-04-25T14:00:00+00:00",
                    digests=(da,),
                    status="failed",
                    target=TimestampTarget.CONTENT,
                    attempts=1,
                ),
            ),
            verify_jobs=(VerifyJob(job_id="job-fail", digests=(da,), status="FAILED"),),
        )

        r = runner.invoke(
            app,
            _stage_args_json(empty_sha256_root, staging, "status", "obj:s-pend-fail"),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        data = json.loads(r.output)
        assert data["next_action_kind"] == "retry_content"
        assert data["counts"]["failed"] >= 1

    def test_status_content_upgraded_next_timestamp_inventory(
        self,
        runner: CliRunner,
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
            "obj:s-cu",
            ("a.txt", b"alpha"),
        )
        da = digests["a.txt"]
        _set_phase(
            sd,
            Phase.CONTENT_UPGRADED,
            pending_digests=(da,),
            proofs={da: verified_content_proof(da)},
        )

        r = runner.invoke(
            app, _stage_args_json(empty_sha256_root, staging, "status", "obj:s-cu")
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        data = json.loads(r.output)
        assert data["next_action_kind"] == "timestamp_inventory"
        assert data["counts"]["upgraded"] == 1
        assert data["counts"]["pending"] == 0

    def test_status_inventory_pending_next_upgrade_inventory(
        self,
        runner: CliRunner,
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
            "obj:s-ip",
            ("a.txt", b"alpha"),
        )
        da = digests["a.txt"]
        inv_d = "f" * 64
        unverified_inv = StagedInventory(
            version="v1",
            digest=inv_d,
            job_id="job-inv",
        )
        _set_phase(
            sd,
            Phase.INVENTORY_PENDING,
            pending_digests=(da,),
            proofs={da: verified_content_proof(da)},
            verify_jobs=(
                VerifyJob(job_id="job-inv", digests=(inv_d,), status="IN_PROGRESS"),
            ),
            inventory=unverified_inv,
        )

        r = runner.invoke(
            app, _stage_args_json(empty_sha256_root, staging, "status", "obj:s-ip")
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        data = json.loads(r.output)
        assert data["next_action_kind"] == "upgrade_inventory"

    def test_status_inventory_pending_with_failed_inv_job_next_retry(
        self,
        runner: CliRunner,
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
            "obj:s-ip-fail",
            ("a.txt", b"alpha"),
        )
        da = digests["a.txt"]
        inv_d = "e" * 64
        unverified_inv = StagedInventory(
            version="v1",
            digest=inv_d,
            job_id="job-inv-fail",
        )
        _set_phase(
            sd,
            Phase.INVENTORY_PENDING,
            pending_digests=(da,),
            proofs={da: verified_content_proof(da)},
            verify_jobs=(
                VerifyJob(job_id="job-inv-fail", digests=(inv_d,), status="FAILED"),
            ),
            inventory=unverified_inv,
        )

        r = runner.invoke(
            app,
            _stage_args_json(empty_sha256_root, staging, "status", "obj:s-ip-fail"),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        data = json.loads(r.output)
        assert data["next_action_kind"] == "retry_inventory"

    def test_status_inventory_upgraded_next_commit(
        self,
        runner: CliRunner,
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
            "obj:s-iu",
            ("a.txt", b"alpha"),
        )
        da = digests["a.txt"]
        inv_d = "d" * 64
        _set_phase(
            sd,
            Phase.INVENTORY_UPGRADED,
            pending_digests=(da,),
            proofs={da: verified_content_proof(da)},
            inventory=_verified_inv_proof("v1", inv_d),
        )

        r = runner.invoke(
            app, _stage_args_json(empty_sha256_root, staging, "status", "obj:s-iu")
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        data = json.loads(r.output)
        assert data["next_action_kind"] == "commit"

    def test_status_committed_next_none(
        self,
        runner: CliRunner,
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
            "obj:s-cm",
            ("a.txt", b"alpha"),
        )
        da = digests["a.txt"]
        inv_d = "c" * 64
        _set_phase(
            sd,
            Phase.COMMITTED,
            pending_digests=(da,),
            proofs={da: verified_content_proof(da)},
            inventory=_verified_inv_proof("v1", inv_d),
        )

        r = runner.invoke(
            app, _stage_args_json(empty_sha256_root, staging, "status", "obj:s-cm")
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        data = json.loads(r.output)
        assert data["next_action_kind"] == "none"

    def test_status_json_schema_v1_invariants(
        self,
        runner: CliRunner,
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
        new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:s-schema",
            ("a.txt", b"alpha"),
        )

        r = runner.invoke(
            app,
            _stage_args_json(empty_sha256_root, staging, "status", "obj:s-schema"),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        data = json.loads(r.output)
        assert isinstance(data, dict)
        assert data["schema_version"] == 1
        assert data["extension"] == "NNNN-ots-timestamps"
        assert isinstance(data["changes"], list)
        assert data["changes"] == [{"path": "a.txt", "action": "added"}]
        counts = data["counts"]
        assert isinstance(counts, dict)
        for key in ("total", "submitted", "upgraded", "pending", "failed"):
            assert key in counts, key
            assert isinstance(counts[key], int)

    def test_status_cache_bytes_sums_ots_cache_files(
        self,
        runner: CliRunner,
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
        sd, _ = new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:s-cache",
            ("a.txt", b"alpha"),
        )
        cache_dir = sd / "ots-cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        sizes = (10, 100, 1000)
        for i, size in enumerate(sizes):
            (cache_dir / f"f{i}.ots").write_bytes(b"x" * size)

        r = runner.invoke(
            app, _stage_args_json(empty_sha256_root, staging, "status", "obj:s-cache")
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        data = json.loads(r.output)
        assert data["cache_bytes"] == sum(sizes)

    def test_status_cache_bytes_zero_when_no_cache_dir(
        self,
        runner: CliRunner,
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
        sd, _ = new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:s-nocache",
            ("a.txt", b"alpha"),
        )
        # Sanity: no ots-cache dir.
        assert not (sd / "ots-cache").exists()

        r = runner.invoke(
            app,
            _stage_args_json(empty_sha256_root, staging, "status", "obj:s-nocache"),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        data = json.loads(r.output)
        assert data["cache_bytes"] == 0

    def test_status_no_lock_acquired(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``stage status`` is documented as lock-free (bounded staleness is
        correct semantics for an inspector): it answers while another
        process holds the staging ``.lock``, with a lock timeout that would
        fail any command that waits for the lock.
        """
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)
        inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _ = new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:s-nolock",
            ("a.txt", b"alpha"),
        )

        runner_obj(runner)["lock_timeout"] = 0.01
        with held_flock(sd / ".lock"):
            r = runner.invoke(
                app,
                _stage_args_json(empty_sha256_root, staging, "status", "obj:s-nolock"),
            )
        assert r.exit_code == ExitCode.SUCCESS, r.output

    def test_status_human_output_dense(
        self,
        runner: CliRunner,
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
            "obj:s-human",
            ("a.txt", b"alpha"),
        )
        da = digests["a.txt"]
        _set_phase(
            sd,
            Phase.CONTENT_PENDING,
            pending_digests=(da,),
            submissions=(
                Submission(
                    submitted_at="2026-04-25T12:00:00+00:00",
                    digests=(da,),
                    status="submitted",
                    target=TimestampTarget.CONTENT,
                    attempts=1,
                ),
            ),
            verify_jobs=(
                VerifyJob(job_id="job-h", digests=(da,), status="IN_PROGRESS"),
            ),
        )

        r = runner.invoke(
            app, stage_args(empty_sha256_root, staging, "status", "obj:s-human")
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        out = r.output
        assert "Object:" in out
        assert "Metadata:" in out
        assert "Workflow:" in out
        assert "Network:" in out
        assert "a.txt\n\nMetadata:" in out
        assert "user.address: missing\n\nWorkflow:" in out
        assert "Next: ocfl stage upgrade obj:s-human --watch" in out
        assert "Network:\n  Bitcoin:" in out
        assert "A a.txt" in out
        assert "Phase:" in out
        assert "Bitcoin:" in out
        assert "Next:" in out
        assert "job-h" in out
        assert "ago" in out

    def test_status_humanize_age_recent_seconds(
        self,
        runner: CliRunner,
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
            "obj:s-h-sec",
            ("a.txt", b"alpha"),
        )
        da = digests["a.txt"]

        from datetime import UTC, datetime, timedelta

        fake_now = datetime(2026, 4, 25, 14, 0, 30, tzinfo=UTC)
        submitted = fake_now - timedelta(seconds=30)
        submitted_iso = submitted.isoformat(timespec="seconds")

        runner_obj(runner)["clock"] = FakeClock(fake_now)

        _set_phase(
            sd,
            Phase.CONTENT_PENDING,
            pending_digests=(da,),
            submissions=(
                Submission(
                    submitted_at=submitted_iso,
                    digests=(da,),
                    status="submitted",
                    target=TimestampTarget.CONTENT,
                    attempts=1,
                ),
            ),
            verify_jobs=(
                VerifyJob(job_id="job-sec", digests=(da,), status="IN_PROGRESS"),
            ),
        )

        r = runner.invoke(
            app, stage_args(empty_sha256_root, staging, "status", "obj:s-h-sec")
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert "30s ago" in r.output

    def test_status_humanize_age_hours(
        self,
        runner: CliRunner,
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
        r = runner.invoke(
            app,
            stage_args(empty_sha256_root, staging, "new", "obj:s-h-hr", "--ots"),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        src = tmp_path / "a.txt"
        src.write_bytes(b"alpha")
        r = runner.invoke(
            app, stage_args(empty_sha256_root, staging, "add", "obj:s-h-hr", str(src))
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output

        from datetime import UTC, datetime, timedelta

        fake_now = datetime(2026, 4, 25, 14, 0, 0, tzinfo=UTC)
        submitted = fake_now - timedelta(hours=2)
        clock = FakeClock(submitted)
        runner_obj(runner)["clock"] = clock

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:s-h-hr",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        sd = staging_dir(empty_sha256_root, staging, "obj:s-h-hr")

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "upgrade",
                "obj:s-h-hr",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.PENDING, r.output
        assert load_ots(sd).verify_jobs[0].status == "IN_PROGRESS"

        clock.set(fake_now)
        r = runner.invoke(
            app, stage_args(empty_sha256_root, staging, "status", "obj:s-h-hr")
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert "2h ago" in r.output

    def test_status_bitcoin_in_sync_field_true_when_node_synced(
        self,
        runner: CliRunner,
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
        new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:s-btc",
            ("a.txt", b"alpha"),
        )

        r = runner.invoke(
            app, _stage_args_json(empty_sha256_root, staging, "status", "obj:s-btc")
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        data = json.loads(r.output)
        bitcoin = data["bitcoin"]
        assert isinstance(bitcoin, dict)
        assert bitcoin["in_sync"] is True
        assert isinstance(bitcoin["height"], int)
        assert isinstance(bitcoin["tip_hash"], str)

    def test_status_bitcoin_section_null_when_no_bitcoin_config(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """OTS staging exists but [bitcoin] absent from config: status MUST
        emit ``bitcoin: null`` (no RPC call). [simpleproof] still configured.

        Pinning the [bitcoin]-absent branch: see step-17 spec deliberation
        — when [bitcoin] is missing, status returns ``bitcoin=null`` rather
        than failing or making an RPC call. The OTS staging itself was
        created earlier with [bitcoin] present (required by ``stage new
        --ots``); we then drop [bitcoin] from config before invoking status.
        """
        write_config(tmp_path, monkeypatch)  # full config to enable `stage new --ots`
        inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:s-btc-null",
            ("a.txt", b"alpha"),
        )

        # Now drop [bitcoin] from config; [simpleproof] stays.
        write_config(tmp_path, monkeypatch, body=SP_OK)

        r = runner.invoke(
            app,
            _stage_args_json(empty_sha256_root, staging, "status", "obj:s-btc-null"),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        data = json.loads(r.output)
        assert data["bitcoin"] is None

    def test_status_simpleproof_configured_reflects_config(
        self,
        runner: CliRunner,
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
        new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:s-sp",
            ("a.txt", b"alpha"),
        )

        r = runner.invoke(
            app, _stage_args_json(empty_sha256_root, staging, "status", "obj:s-sp")
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        data = json.loads(r.output)
        sp_section = data["simpleproof"]
        assert isinstance(sp_section, dict)
        assert sp_section["configured"] is True
        assert sp_section["category_id"] == CATEGORY_ID

    def test_status_confirmations_required_from_config(
        self,
        runner: CliRunner,
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
        new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:s-conf",
            ("a.txt", b"alpha"),
        )

        r = runner.invoke(
            app, _stage_args_json(empty_sha256_root, staging, "status", "obj:s-conf")
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        data = json.loads(r.output)
        assert data["bitcoin"]["confirmations_required"] == 3


class TestStageStatusNonOtsFallback:
    def test_status_non_ots_text_unchanged(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)

        staging = tmp_path / "staging"
        r = runner.invoke(
            app, stage_args(empty_sha256_root, staging, "new", "obj:non-ots")
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output

        src = tmp_path / "src.txt"
        src.write_text("hello")
        r = runner.invoke(
            app,
            stage_args(empty_sha256_root, staging, "add", "obj:non-ots", str(src)),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output

        r = runner.invoke(
            app, stage_args(empty_sha256_root, staging, "status", "obj:non-ots")
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert "src.txt" in r.output

    def test_status_non_ots_json_wraps_changes_with_extension_null(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)

        staging = tmp_path / "staging"
        r = runner.invoke(
            app, stage_args(empty_sha256_root, staging, "new", "obj:non-ots-json")
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        src = tmp_path / "src.txt"
        src.write_text("hello")
        r = runner.invoke(
            app,
            stage_args(empty_sha256_root, staging, "add", "obj:non-ots-json", str(src)),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output

        r = runner.invoke(
            app,
            _stage_args_json(empty_sha256_root, staging, "status", "obj:non-ots-json"),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        data = json.loads(r.output)
        assert isinstance(data, dict)
        assert data["extension"] is None
        assert isinstance(data["changes"], list)
        # OTS-only keys MUST NOT appear in the non-OTS shape.
        for key in (
            "schema_version",
            "phase",
            "counts",
            "outstanding_jobs",
            "bitcoin",
            "simpleproof",
            "cache_bytes",
            "next_action_kind",
            "next_action",
        ):
            assert key not in data, f"non-OTS JSON must omit {key!r}"
        for entry in data["changes"]:
            assert "path" in entry
            assert "action" in entry
