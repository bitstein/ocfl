"""Tests for `stage timestamp content --retry`.

Pins the FAILED-only resubmit contract from `OTS workflow contract`:

- New flag `--retry` on `stage timestamp content` (boolean; default False).
- Legal only at phase ``content-pending`` (per FSM matrix). Any other phase
  ⇒ ``PhaseError`` ⇒ exit 3.
- ``--retry`` is mutually exclusive with ``--upgrade`` and ``--watch``;
  the CLI rejects the combination at the surface (exit 2) before the FSM.
- For each ``Submission`` with ``status == "failed"``: re-POST those digests
  to SP via ``sp.submit_hashes(digests, category_id)``, increment
  ``attempts``, flip ``status`` back to ``"submitted"`` (crash-safe write
  protocol: ``submitting`` intent → POST → ``submitted``).
- Submissions with ``status`` of ``"submitted"`` or ``"submitting"`` are
  NOT touched (no second POST, ``attempts`` unchanged, ``submitted_at``
  unchanged).
- Empty failed-set ⇒ noop (exit 0, no SP calls, ots.json byte-equal).
- Phase remains ``content-pending`` (FSM ``stay``).

These tests are RED until the ``--retry`` flag lands. The diagnostic is
``No such option: '--retry'`` from typer (exit 2, but for the wrong
reason — the flag does not exist).
"""

import hashlib
from pathlib import Path

import pytest
from fakes import FakeSimpleProof
from msgspec.structs import replace
from runner_helpers import runner_obj
from typer.testing import CliRunner

from ocfl_cli.main import app
from ocfl_cli.model.ots_state import Submission
from ocfl_cli.model.phase import Phase
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
    """Inject *fake* via ``runner.obj["sp_client_factory"]``."""
    runner_obj(runner)["sp_client_factory"] = lambda cfg: fake


# --------------------------------------------------------------------------- #
# Storage-root fixture
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
# OTS staging setup helpers
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


# =========================================================================== #
# Layer-2: --retry resubmits ONLY failed; touches nothing else.
# =========================================================================== #


class TestRetryResubmitsOnlyFailed:
    def test_retry_resubmits_only_failed(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Three submissions: failed, submitted, submitting. --retry POSTs
        only the failed entry's digests; the other two are byte-identical
        afterwards. Phase stays content-pending."""
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
            job_id=None,
            attempts=1,
        )
        submitted = Submission(
            submitted_at="2026-04-23T16:31:00+00:00",
            digests=(d3,),
            status="submitted",
            job_id=None,
            attempts=1,
        )
        submitting = Submission(
            submitted_at="2026-04-23T16:32:00+00:00",
            digests=(d4,),
            status="submitting",
            job_id=None,
            attempts=2,
        )
        _set_state(
            sd,
            phase=Phase.CONTENT_PENDING,
            pending_digests=(d1, d2, d3, d4),
            submissions=(failed, submitted, submitting),
        )

        # Snapshot the pre-retry SP submission count (should be 0 from fixtures).
        pre_calls = list(sp_fake._submissions)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "content",
                "obj:retry-only-failed",
                "--retry",
            ),
        )
        assert r.exit_code == 0, r.output

        # Exactly one new SP call, carrying ONLY the failed entry's digests.
        new_calls = sp_fake._submissions[len(pre_calls) :]
        assert len(new_calls) == 1, new_calls
        assert tuple(new_calls[0].digests) == (d1, d2)

        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_PENDING

        by_first = {s.digests[0]: s for s in state.submissions}

        # Failed entry: status flipped to submitted, attempts incremented.
        retried = by_first[d1]
        assert retried.status == "submitted"
        assert retried.attempts > failed.attempts
        assert retried.digests == (d1, d2)

        # Submitted entry: completely untouched.
        assert by_first[d3] == submitted
        # Submitting entry: completely untouched.
        assert by_first[d4] == submitting


# =========================================================================== #
# Layer-2: empty failed-set is a noop.
# =========================================================================== #


class TestRetryEmptyFailedNoop:
    def test_retry_empty_failed_set_is_noop(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No failed submissions: --retry exits 0, posts nothing, leaves
        ots.json byte-equal, and prints a user-friendly notice."""
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
            job_id=None,
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
                "timestamp",
                "content",
                "obj:retry-empty",
                "--retry",
            ),
        )
        assert r.exit_code == 0, r.output

        # No SP POSTs.
        assert len(sp_fake._submissions) == before_calls
        # ots.json byte-equal.
        assert ots_json.read_bytes() == before_bytes
        # User-friendly mention of "failed" in stdout/stderr.
        assert "failed" in r.output.lower()


# =========================================================================== #
# Layer-2: multiple failed entries each get their own POST.
# =========================================================================== #


class TestRetryMultipleFailedIndependent:
    def test_retry_multiple_failed_resubmits_each_independently(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two disjoint failed submissions ⇒ two SP POSTs; both flip to
        submitted; both attempts incremented."""
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
            job_id=None,
            attempts=1,
        )
        failed_b = Submission(
            submitted_at="2026-04-23T16:31:00+00:00",
            digests=(db,),
            status="failed",
            job_id=None,
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
                "timestamp",
                "content",
                "obj:retry-multi",
                "--retry",
            ),
        )
        assert r.exit_code == 0, r.output

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


# =========================================================================== #
# Layer-2: --retry at any phase other than content-pending raises PhaseError.
# =========================================================================== #


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
        """All phases ∉ {content-pending} ⇒ non-zero exit with the wrong
        phase mentioned in stderr/stdout."""
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
            job_id=None,
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
                "timestamp",
                "content",
                "obj:retry-phase",
                "--retry",
            ),
        )
        assert r.exit_code != 0, r.output
        # Wrong-phase value should surface in the diagnostic.
        assert phase.value in r.output
        # No SP POSTs on rejection.
        assert len(sp_fake._submissions) == pre_calls


# =========================================================================== #
# Layer-2: --retry rejects --upgrade / --watch combinations at CLI surface.
# =========================================================================== #


class TestRetryFlagCombinationGuards:
    @pytest.mark.parametrize(
        "extra_flag",
        ["--upgrade", "--watch"],
    )
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
        surface with non-zero exit and an actionable message."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, _paths, digests = _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:retry-combo",
            ("a.txt", b"alpha"),
        )
        d = digests["a.txt"]

        failed = Submission(
            submitted_at="2026-04-23T16:30:00+00:00",
            digests=(d,),
            status="failed",
            job_id=None,
            attempts=1,
        )
        _set_state(
            sd,
            phase=Phase.CONTENT_PENDING,
            pending_digests=(d,),
            submissions=(failed,),
        )

        pre_calls = len(sp_fake._submissions)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "content",
                "obj:retry-combo",
                "--retry",
                extra_flag,
            ),
        )
        assert r.exit_code != 0, r.output
        # The diagnostic must name BOTH conflicting flags so the user knows
        # which combination is rejected (vs. the typer "no such option"
        # error that names only --retry).
        assert "--retry" in r.output
        assert extra_flag in r.output
        # Rejected before any SP POST.
        assert len(sp_fake._submissions) == pre_calls


# =========================================================================== #
# Layer-2: crash-safe submit protocol — final state shape after clean retry.
# =========================================================================== #


class TestRetryCrashSafeShape:
    def test_retry_crash_safe_resubmit_intent(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A clean --retry over a single failed submission demonstrates the
        crash-safe write protocol shape: attempts bumped at least once and
        the entry lands at status=submitted (intent → POST → submitted).

        Fault-injection coverage of the same code lives in the submit-path
        tests; --retry reuses the same protocol, so this asserts the
        end-state shape is consistent."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        sd, _paths, digests = _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:retry-shape",
            ("a.txt", b"alpha"),
        )
        d = digests["a.txt"]

        failed = Submission(
            submitted_at="2026-04-23T16:30:00+00:00",
            digests=(d,),
            status="failed",
            job_id=None,
            attempts=1,
        )
        _set_state(
            sd,
            phase=Phase.CONTENT_PENDING,
            pending_digests=(d,),
            submissions=(failed,),
        )

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "content",
                "obj:retry-shape",
                "--retry",
            ),
        )
        assert r.exit_code == 0, r.output

        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_PENDING
        # Exactly one Submission for this digest set in the post state.
        retried = [s for s in state.submissions if s.digests == (d,)]
        assert len(retried) == 1
        s = retried[0]
        assert s.status == "submitted"
        assert s.attempts > failed.attempts
