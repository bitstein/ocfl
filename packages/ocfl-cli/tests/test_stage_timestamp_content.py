"""Tests for `ocfl stage timestamp content` (OTS workflow step 10).

Pins step 10 of `OTS workflow contract`:

- Compute candidate digests:
  ``non_ots_digests(stage.state) - set(obj.inventory.manifest)``.
- Subtract already-submitted/already-upgraded
  (``ots.json.pending_digests`` + ``content_proofs`` keys). Remainder is
  this round's submission set.
- Extend ``ots.json.pending_digests`` with the submission set.
- Delete/rename-only no-op: empty submission set AND empty
  ``pending_digests`` → print "nothing to timestamp; advancing to
  content-upgraded" and advance phase to ``content-upgraded``.
- Crash-safe submit protocol: persist ``Submission(status="submitting")``
  intent → POST → update entry to ``submitted``.
- POST in chunks of 1000 (``SimpleProofClient.submit_hashes`` chunks).
- Phase → ``content-pending`` on success (via ``check_transition``).
- Crash-safe resubmit: any ``submitting`` intent persisted across
  process restart is resubmitted on next run; SP records duplicates
  without failure (`crash_safe_submit_protocol.on_startup`).

The CLI command is the new ``stage timestamp content <object_id>``
subcommand under ``stage_app``. Tests inject a ``FakeSimpleProof``
via ``runner.obj["sp_client_factory"]`` (see ``conftest.py::runner``
for the auto-attach mechanism).
"""

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from fakes import FakeSimpleProof
from msgspec.structs import replace
from runner_helpers import runner_obj
from typer.testing import CliRunner

from ocfl_cli.main import app
from ocfl_cli.stage.fsm import Phase
from ocfl_cli.stage.state import (
    Submission,
    load_ots,
    now_utc_iso,
    save_ots,
)
from ocfl_cli.stage.store import StagingStore
from ocfl_lib import (
    DigestAlgorithm,
    LocalFS,
    Spec,
    StorageRoot,
    get_layout,
    stage_dir,
)

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
    """Inject *fake* via ``runner.obj["sp_client_factory"]``.

    The runner fixture (conftest) auto-passes ``runner.obj`` as the
    typer-context obj on every ``runner.invoke``; service consumes the
    factory on its kwargs.
    """
    runner_obj(runner)["sp_client_factory"] = lambda cfg: fake


# --------------------------------------------------------------------------- #
# Storage-root fixtures (SHA-256 — `--ots` requires it)
# --------------------------------------------------------------------------- #


@pytest.fixture
def empty_sha256_root(tmp_path: Path) -> Path:
    """Initialized storage root with no objects."""
    location = tmp_path / "root"
    location.mkdir()
    fs = LocalFS(location)
    layout = get_layout("0002-flat-direct-storage-layout")
    StorageRoot.init(fs, fs, spec=Spec.V1_1, layout=layout)
    return location


@pytest.fixture
def sha256_root_with_v1(tmp_path: Path) -> Path:
    """Storage root with `obj:test` committed at v1 using sha256.

    Used for the carry-forward subtraction case: a digest already in the
    committed object's manifest must NOT be resubmitted by `timestamp content`.
    """
    location = tmp_path / "root"
    location.mkdir()
    fs = LocalFS(location)
    layout = get_layout("0002-flat-direct-storage-layout")
    root = StorageRoot.init(fs, fs, spec=Spec.V1_1, layout=layout)

    obj_dir = location / "src"
    obj_dir.mkdir()
    (obj_dir / "carry.txt").write_text("carry-over")
    stage = stage_dir(fs, "src", DigestAlgorithm.SHA256)
    root.commit(fs, "obj:test", stage, message="v1")
    shutil.rmtree(obj_dir)
    return location


# --------------------------------------------------------------------------- #
# Layer-2: full submit flow
# --------------------------------------------------------------------------- #


class TestFullSubmitFlow:
    def test_basic_submit_advances_to_content_pending(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fresh OTS staging + stage add → timestamp content submits, phase=CP."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"

        r = runner.invoke(
            app,
            _stage_args(empty_sha256_root, staging, "new", "obj:new", "--ots"),
        )
        assert r.exit_code == 0, r.output

        f1 = tmp_path / "a.txt"
        f1.write_text("alpha")
        f2 = tmp_path / "b.txt"
        f2.write_text("bravo")
        for src in (f1, f2):
            r = runner.invoke(
                app,
                _stage_args(empty_sha256_root, staging, "add", "obj:new", str(src)),
            )
            assert r.exit_code == 0, r.output

        r = runner.invoke(
            app,
            _stage_args(empty_sha256_root, staging, "timestamp", "content", "obj:new"),
        )
        assert r.exit_code == 0, r.output

        sd = _staging_dir(empty_sha256_root, staging, "obj:new")
        state = load_ots(sd)

        assert state.phase is Phase.CONTENT_PENDING
        d1 = hashlib.sha256(b"alpha").hexdigest()
        d2 = hashlib.sha256(b"bravo").hexdigest()
        # pending_digests is a durable target snapshot; extended with submission set.
        assert set(state.pending_digests) == {d1, d2}
        # Exactly one Submission entry, finalized as `submitted`.
        assert len(state.submissions) == 1
        sub = state.submissions[0]
        assert sub.status == "submitted"
        assert set(sub.digests) == {d1, d2}
        assert sub.attempts >= 1
        # Fake SP recorded one chunked POST with both digests + correct category.
        assert len(sp_fake._submissions) == 1
        recorded = sp_fake._submissions[0]
        assert set(recorded.digests) == {d1, d2}
        assert recorded.category_id == 641

    def test_carry_forward_digests_excluded_from_submission(
        self,
        runner: CliRunner,
        sha256_root_with_v1: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Digest already in committed `obj.inventory.manifest` not resubmitted."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        r = runner.invoke(
            app,
            _stage_args(sha256_root_with_v1, staging, "new", "obj:test", "--ots"),
        )
        assert r.exit_code == 0, r.output

        # Re-add carry-forward content + one new file.
        carry = tmp_path / "carry.txt"
        carry.write_text("carry-over")
        new = tmp_path / "new.txt"
        new.write_text("brand-new")
        runner.invoke(
            app,
            _stage_args(sha256_root_with_v1, staging, "add", "obj:test", str(carry)),
        )
        runner.invoke(
            app,
            _stage_args(sha256_root_with_v1, staging, "add", "obj:test", str(new)),
        )

        r = runner.invoke(
            app,
            _stage_args(
                sha256_root_with_v1, staging, "timestamp", "content", "obj:test"
            ),
        )
        assert r.exit_code == 0, r.output

        sd = _staging_dir(sha256_root_with_v1, staging, "obj:test")
        state = load_ots(sd)
        new_d = hashlib.sha256(b"brand-new").hexdigest()
        carry_d = hashlib.sha256(b"carry-over").hexdigest()

        assert state.phase is Phase.CONTENT_PENDING
        assert state.pending_digests == (new_d,)
        assert carry_d not in state.pending_digests
        assert len(state.submissions) == 1
        assert state.submissions[0].digests == (new_d,)
        # SP saw exactly the new digest, never the carry-forward one.
        assert len(sp_fake._submissions) == 1
        assert sp_fake._submissions[0].digests == (new_d,)


# --------------------------------------------------------------------------- #
# Layer-2: delete/rename-only no-op
# --------------------------------------------------------------------------- #


class TestDeleteRenameOnlyNoop:
    def test_empty_submission_and_empty_pending_advances_to_upgraded(
        self,
        runner: CliRunner,
        sha256_root_with_v1: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stage state ⊆ obj.inventory.manifest, pending_digests=() →
        prints `nothing to timestamp; advancing to content-upgraded`,
        advances phase to ``content-upgraded``, no SP calls."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        r = runner.invoke(
            app,
            _stage_args(sha256_root_with_v1, staging, "new", "obj:test", "--ots"),
        )
        assert r.exit_code == 0, r.output

        # Re-add the same carry-forward content under a NEW logical path:
        # the stage state's only digest is already in the committed manifest.
        carry = tmp_path / "carry.txt"
        carry.write_text("carry-over")
        r = runner.invoke(
            app,
            _stage_args(
                sha256_root_with_v1,
                staging,
                "add",
                "obj:test",
                str(carry),
                "--as",
                "renamed.txt",
            ),
        )
        assert r.exit_code == 0, r.output

        r = runner.invoke(
            app,
            _stage_args(
                sha256_root_with_v1, staging, "timestamp", "content", "obj:test"
            ),
        )
        assert r.exit_code == 0, r.output
        combined = (r.output + (r.stderr or "")).lower()
        assert "nothing to timestamp" in combined
        assert "content-upgraded" in combined

        sd = _staging_dir(sha256_root_with_v1, staging, "obj:test")
        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_UPGRADED
        assert state.pending_digests == ()
        assert state.submissions == ()
        # No SP traffic.
        assert sp_fake._submissions == []


# --------------------------------------------------------------------------- #
# Layer-2: chunking boundary (999 / 1000 / 1001)
# --------------------------------------------------------------------------- #


def _seed_sha256(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _seed_many(n: int) -> list[str]:
    return [_seed_sha256(f"d-{i}") for i in range(n)]


def _seed_inventory_with_digests(staging_dir: Path, digests: list[str]) -> None:
    """Rewrite the staged inventory's head state to reference exactly *digests*.

    Builds N synthetic logical paths (`f"path/{i}.bin"`) keyed off the digest
    list, in stage state and manifest. Bypasses real ingest (no actual file
    bytes) — the candidate-digest computation in `timestamp content` reads
    only the inventory state and the prior committed manifest.
    """
    inv_path = staging_dir / "inventory.json"
    inv = json.loads(inv_path.read_bytes())
    head = inv["head"]
    state: dict[str, list[str]] = {}
    manifest: dict[str, list[str]] = {}
    for i, d in enumerate(digests):
        logical = f"path/{i}.bin"
        content = f"{head}/content/{i}.bin"
        state.setdefault(d, []).append(logical)
        manifest.setdefault(d, []).append(content)
    inv["versions"][head]["state"] = state
    inv["manifest"] = manifest
    inv_path.write_text(json.dumps(inv))


class TestChunkingBoundary:
    @pytest.mark.parametrize(
        ("n", "expected_chunk_count"),
        [(999, 1), (1000, 1), (1001, 2)],
    )
    def test_submit_chunks_at_1000(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        n: int,
        expected_chunk_count: int,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        r = runner.invoke(
            app,
            _stage_args(empty_sha256_root, staging, "new", "obj:big", "--ots"),
        )
        assert r.exit_code == 0, r.output

        sd = _staging_dir(empty_sha256_root, staging, "obj:big")
        digests = _seed_many(n)
        _seed_inventory_with_digests(sd, digests)

        r = runner.invoke(
            app,
            _stage_args(empty_sha256_root, staging, "timestamp", "content", "obj:big"),
        )
        assert r.exit_code == 0, r.output

        # FakeSimpleProof.submit_hashes records ONE _Submission per chunked
        # call. The real client chunks at 1000 internally; the fake records
        # whatever it was handed. So if the production code calls
        # `submit_hashes(all_digests)` once (and the fake records once even
        # for a 1001-element list), this test will fail. The team-lead's
        # spec bullet "FakeSimpleProof recorded the chunked calls" expects
        # the production path to invoke the fake once per chunk.
        assert len(sp_fake._submissions) == expected_chunk_count
        assert sum(len(s.digests) for s in sp_fake._submissions) == n

        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_PENDING
        assert set(state.pending_digests) == set(digests)


# --------------------------------------------------------------------------- #
# Layer-2: crash-safe resubmit + double-submit benign
# --------------------------------------------------------------------------- #


def _seed_submitting_intent(staging_dir: Path, digests: tuple[str, ...]) -> None:
    """Persist ots.json with phase=content-pending and a `submitting` entry.

    Simulates a process that crashed after writing the intent but before
    the SP POST returned (or before persisting `submitted`). On the next
    `stage timestamp content`, the resubmit-on-startup contract requires
    the entry to be POSTed again.
    """
    state = load_ots(staging_dir)
    state = replace(
        state,
        phase=Phase.CONTENT_PENDING,
        pending_digests=digests,
        submissions=(
            Submission(
                submitted_at=now_utc_iso(),
                digests=digests,
                status="submitting",
                job_id=None,
                attempts=0,
            ),
        ),
    )
    save_ots(staging_dir, state)


class TestCrashSafeResubmit:
    def test_submitting_intent_is_resubmitted_on_next_run(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`submitting` entry persisted across crash → next run resubmits."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        r = runner.invoke(
            app,
            _stage_args(empty_sha256_root, staging, "new", "obj:crash", "--ots"),
        )
        assert r.exit_code == 0, r.output

        sd = _staging_dir(empty_sha256_root, staging, "obj:crash")
        digests = tuple(_seed_many(3))
        _seed_inventory_with_digests(sd, list(digests))
        _seed_submitting_intent(sd, digests)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root, staging, "timestamp", "content", "obj:crash"
            ),
        )
        assert r.exit_code == 0, r.output

        # SP got a fresh submission for the resubmit (double-submit benign per
        # `crash_safe_submit_protocol.on_startup`).
        assert len(sp_fake._submissions) >= 1
        recorded_digests = {d for s in sp_fake._submissions for d in s.digests}
        assert recorded_digests == set(digests)

        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_PENDING
        # Every `submitting` intent must be cleared (resolved to `submitted`
        # or replaced) — leaving a stale `submitting` entry would loop on
        # every subsequent run.
        assert all(s.status != "submitting" for s in state.submissions)


class TestDoubleSubmitBenign:
    def test_double_submit_does_not_fail_and_records_duplicate(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two `submitting` intents (e.g., crashed twice) → both resubmit;
        FakeSimpleProof records duplicates without failure."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        r = runner.invoke(
            app,
            _stage_args(empty_sha256_root, staging, "new", "obj:dup", "--ots"),
        )
        assert r.exit_code == 0, r.output

        sd = _staging_dir(empty_sha256_root, staging, "obj:dup")
        digests = tuple(_seed_many(2))
        _seed_inventory_with_digests(sd, list(digests))

        # Round 1: seed + run.
        _seed_submitting_intent(sd, digests)
        r = runner.invoke(
            app,
            _stage_args(empty_sha256_root, staging, "timestamp", "content", "obj:dup"),
        )
        assert r.exit_code == 0, r.output
        round_1_count = len(sp_fake._submissions)
        assert round_1_count >= 1

        # Round 2: simulate a SECOND crash → re-seed `submitting` → run again.
        _seed_submitting_intent(sd, digests)
        r = runner.invoke(
            app,
            _stage_args(empty_sha256_root, staging, "timestamp", "content", "obj:dup"),
        )
        assert r.exit_code == 0, r.output

        # SP recorded a strictly larger number of submissions — duplicates ok.
        assert len(sp_fake._submissions) > round_1_count
        # Every recorded SP submission carries exactly the same digests.
        for s in sp_fake._submissions:
            assert set(s.digests) == set(digests)

        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_PENDING
        assert all(s.status != "submitting" for s in state.submissions)
