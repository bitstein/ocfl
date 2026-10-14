from pathlib import Path

import pytest
from fakes import FakeSimpleProof
from msgspec.structs import replace
from ots_helpers import inject_fake_sp, new_ots_staging, write_config
from runner_helpers import stage_args
from typer.testing import CliRunner

from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app
from ocfl_cli.model.ots_state import Submission
from ocfl_cli.model.phase import Phase, TimestampTarget
from ocfl_cli.store.ots_state import load_ots, save_ots


def _set_state(
    sd: Path,
    *,
    phase: Phase,
    pending_digests: tuple[str, ...],
    submissions: tuple[Submission, ...],
) -> None:
    state = load_ots(sd)
    state = replace(
        state,
        phase=phase,
        pending_digests=pending_digests,
        submissions=submissions,
    )
    save_ots(sd, state)


class TestRetryResubmitsOnlyFailed:
    def test_retry_resubmits_only_failed(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, digests = new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:retry-only-failed",
            ("a.txt", b"alpha"),
            ("b.txt", b"bravo"),
            ("c.txt", b"charlie"),
            ("d.txt", b"delta"),
        )
        d1 = digests["a.txt"]
        d2 = digests["b.txt"]
        d3 = digests["c.txt"]
        d4 = digests["d.txt"]

        failed = Submission(
            submitted_at="2026-04-23T16:30:00+00:00",
            digests=(d1, d2),
            status="failed",
            target=TimestampTarget.CONTENT,
            attempts=1,
        )
        submitted = Submission(
            submitted_at="2026-04-23T16:31:00+00:00",
            digests=(d3,),
            status="submitted",
            target=TimestampTarget.CONTENT,
            attempts=1,
        )
        submitting = Submission(
            submitted_at="2026-04-23T16:32:00+00:00",
            digests=(d4,),
            status="submitting",
            target=TimestampTarget.CONTENT,
            attempts=2,
        )
        _set_state(
            sd,
            phase=Phase.CONTENT_PENDING,
            pending_digests=(d1, d2, d3, d4),
            submissions=(failed, submitted, submitting),
        )

        pre_calls = list(sp_fake._submissions)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "retry",
                "obj:retry-only-failed",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output

        new_calls = sp_fake._submissions[len(pre_calls) :]
        assert len(new_calls) == 1, new_calls
        assert tuple(new_calls[0].digests) == (d1, d2)

        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_PENDING

        by_first = {s.digests[0]: s for s in state.submissions}

        retried = by_first[d1]
        assert retried.status == "submitted"
        assert retried.attempts > failed.attempts
        assert retried.digests == (d1, d2)

        assert by_first[d3] == submitted
        assert by_first[d4] == submitting


class TestRetryEmptyFailedNoop:
    def test_retry_empty_failed_set_is_noop(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, digests = new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:retry-empty",
            ("a.txt", b"alpha"),
        )
        d = digests["a.txt"]

        only = Submission(
            submitted_at="2026-04-23T16:30:00+00:00",
            digests=(d,),
            status="submitted",
            target=TimestampTarget.CONTENT,
            attempts=1,
        )
        _set_state(
            sd,
            phase=Phase.CONTENT_PENDING,
            pending_digests=(d,),
            submissions=(only,),
        )

        ots_json = sd / "ots.json"
        before_bytes = ots_json.read_bytes()
        before_calls = len(sp_fake._submissions)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "retry",
                "obj:retry-empty",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output

        assert len(sp_fake._submissions) == before_calls
        assert ots_json.read_bytes() == before_bytes
        assert "failed" in r.output.lower()


class TestRetryMultipleFailedIndependent:
    def test_retry_multiple_failed_resubmits_each_independently(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, digests = new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:retry-multi",
            ("a.txt", b"alpha"),
            ("b.txt", b"bravo"),
        )
        da = digests["a.txt"]
        db = digests["b.txt"]

        failed_a = Submission(
            submitted_at="2026-04-23T16:30:00+00:00",
            digests=(da,),
            status="failed",
            target=TimestampTarget.CONTENT,
            attempts=1,
        )
        failed_b = Submission(
            submitted_at="2026-04-23T16:31:00+00:00",
            digests=(db,),
            status="failed",
            target=TimestampTarget.CONTENT,
            attempts=2,
        )
        _set_state(
            sd,
            phase=Phase.CONTENT_PENDING,
            pending_digests=(da, db),
            submissions=(failed_a, failed_b),
        )

        pre_calls = list(sp_fake._submissions)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "retry",
                "obj:retry-multi",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output

        new_calls = sp_fake._submissions[len(pre_calls) :]
        assert len(new_calls) == 2, new_calls
        posted = {tuple(c.digests) for c in new_calls}
        assert posted == {(da,), (db,)}

        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_PENDING

        by_first = {s.digests[0]: s for s in state.submissions}
        assert by_first[da].status == "submitted"
        assert by_first[da].attempts > failed_a.attempts
        assert by_first[db].status == "submitted"
        assert by_first[db].attempts > failed_b.attempts


class TestRetryPhaseGate:
    @pytest.mark.parametrize(
        "phase",
        [
            Phase.DRAFTING,
            Phase.CONTENT_UPGRADED,
            Phase.INVENTORY_PENDING,
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
        write_config(tmp_path, monkeypatch)
        inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, digests = new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:retry-phase",
            ("a.txt", b"alpha"),
        )
        d = digests["a.txt"]

        failed = Submission(
            submitted_at="2026-04-23T16:30:00+00:00",
            digests=(d,),
            status="failed",
            target=TimestampTarget.CONTENT,
            attempts=1,
        )
        _set_state(
            sd,
            phase=phase,
            pending_digests=(d,),
            submissions=(failed,),
        )

        pre_calls = len(sp_fake._submissions)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "retry",
                "obj:retry-phase",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code != ExitCode.SUCCESS, r.output
        assert phase.value in r.output
        assert len(sp_fake._submissions) == pre_calls
