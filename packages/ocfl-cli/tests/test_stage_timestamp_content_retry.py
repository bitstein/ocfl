import hashlib
from pathlib import Path

import pytest
from fakes import FakeSimpleProof
from msgspec.structs import replace
from runner_helpers import runner_obj
from typer.testing import CliRunner

from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app
from ocfl_cli.model.ots_state import Submission
from ocfl_cli.model.phase import Phase, TimestampTarget
from ocfl_cli.store.ots_state import load_ots, save_ots
from ocfl_cli.store.staging import StagingStore
from ocfl_lib import LocalFS, Spec, StorageRoot, get_layout

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
    runner_obj(runner)["sp_client_factory"] = lambda cfg: fake


@pytest.fixture
def empty_sha256_root(tmp_path: Path) -> Path:
    location = tmp_path / "root"
    location.mkdir()
    fs = LocalFS(location)
    layout = get_layout("0002-flat-direct-storage-layout")
    StorageRoot.init(fs, fs, spec=Spec.V1_1, layout=layout)
    return location


def _new_ots_staging(
    runner: CliRunner,
    root: Path,
    staging: Path,
    object_id: str,
    *files: tuple[str, bytes],
) -> tuple[Path, dict[str, str], dict[str, str]]:
    """Run ``stage new --ots`` + ``stage add`` for each file.

    Returns ``(staging_dir, path_by_seed, digest_by_seed)``.
    """
    r = runner.invoke(app, _stage_args(root, staging, "new", object_id, "--ots"))
    assert r.exit_code == ExitCode.SUCCESS, r.output

    paths: dict[str, str] = {}
    digests: dict[str, str] = {}
    for name, data in files:
        src = staging.parent / name
        src.write_bytes(data)
        r = runner.invoke(app, _stage_args(root, staging, "add", object_id, str(src)))
        assert r.exit_code == ExitCode.SUCCESS, r.output
        paths[name] = name
        digests[name] = hashlib.sha256(data).hexdigest()

    return _staging_dir(root, staging, object_id), paths, digests


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
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, _paths, digests = _new_ots_staging(
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
            _stage_args(
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
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, _paths, digests = _new_ots_staging(
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
            _stage_args(
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
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, _paths, digests = _new_ots_staging(
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
            _stage_args(
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
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, _paths, digests = _new_ots_staging(
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
            _stage_args(
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
