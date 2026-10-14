import hashlib
import json
import shutil
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
from ocfl_cli.model.ots_state import Submission
from ocfl_cli.model.phase import Phase, TimestampTarget
from ocfl_cli.store.ots_state import load_ots, save_ots, utc_iso
from ocfl_cli.store.staging import StagingStore
from ocfl_cli.system import SYSTEM_CLOCK
from ocfl_cli.workflow import timestamp as wf_timestamp
from ocfl_lib import (
    DigestAlgorithm,
    LocalFS,
    Spec,
    StorageRoot,
    get_layout,
    scan_dir,
)

_SP_OK = """\
[simpleproof]
base_url = "https://stage.example"
proof_category_id = 641
"""

_BTC_OK_COOKIE = """\
[bitcoin]
rpc_url = "http://127.0.0.1:8332"
zmq_endpoint = "tcp://127.0.0.1:28332"
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


@pytest.fixture
def sha256_root_with_v1(tmp_path: Path) -> Path:
    location = tmp_path / "root"
    location.mkdir()
    fs = LocalFS(location)
    layout = get_layout("0002-flat-direct-storage-layout")
    root = StorageRoot.init(fs, fs, spec=Spec.V1_1, layout=layout)

    obj_dir = location / "src"
    obj_dir.mkdir()
    (obj_dir / "carry.txt").write_text("carry-over")
    stage = scan_dir(fs, "src", DigestAlgorithm.SHA256)
    root.commit(fs, "obj:test", stage, message="v1")
    shutil.rmtree(obj_dir)
    return location


class TestFullSubmitFlow:
    def test_basic_submit_advances_to_content_pending(
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
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:new",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == 0, r.output
        assert "Content:" in r.output
        assert "Object: obj:new" in r.output
        assert "Submitted 2 digests to SimpleProof" in r.output
        assert "Workflow:" in r.output
        assert "Phase: drafting -> content-pending" in r.output
        assert "Next: ocfl stage upgrade obj:new --watch" in r.output
        assert "INFO " not in r.output

        sd = _staging_dir(empty_sha256_root, staging, "obj:new")
        state = load_ots(sd)

        assert state.phase is Phase.CONTENT_PENDING
        d1 = hashlib.sha256(b"alpha").hexdigest()
        d2 = hashlib.sha256(b"bravo").hexdigest()
        assert set(state.pending_digests) == {d1, d2}
        assert len(state.submissions) == 1
        sub = state.submissions[0]
        assert sub.status == "submitted"
        assert set(sub.digests) == {d1, d2}
        assert sub.attempts >= 1
        assert len(sp_fake._submissions) == 1
        recorded = sp_fake._submissions[0]
        assert set(recorded.digests) == {d1, d2}
        assert recorded.category_id == 641

    def test_plain_object_digests_are_submitted_for_first_covered_version(
        self,
        runner: CliRunner,
        sha256_root_with_v1: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A plain object proves nothing yet (ADR-0004, `timestampFrom`): a
        digest already in its manifest still needs a proof in the first
        covered version, so it is submitted with the new one."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        r = runner.invoke(
            app,
            _stage_args(sha256_root_with_v1, staging, "new", "obj:test", "--ots"),
        )
        assert r.exit_code == 0, r.output

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
                sha256_root_with_v1,
                staging,
                "timestamp",
                "obj:test",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == 0, r.output

        sd = _staging_dir(sha256_root_with_v1, staging, "obj:test")
        state = load_ots(sd)
        new_d = hashlib.sha256(b"brand-new").hexdigest()
        carry_d = hashlib.sha256(b"carry-over").hexdigest()

        both = tuple(sorted((new_d, carry_d)))
        assert state.phase is Phase.CONTENT_PENDING
        assert state.pending_digests == both
        assert len(state.submissions) == 1
        assert state.submissions[0].digests == both
        assert len(sp_fake._submissions) == 1
        assert sp_fake._submissions[0].digests == both


class TestDeleteRenameOnlyNoop:
    def test_renamed_plain_object_file_still_needs_a_proof(
        self,
        runner: CliRunner,
        sha256_root_with_v1: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)

        staging = tmp_path / "staging"
        r = runner.invoke(
            app,
            _stage_args(sha256_root_with_v1, staging, "new", "obj:test", "--ots"),
        )
        assert r.exit_code == 0, r.output

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
                sha256_root_with_v1,
                staging,
                "timestamp",
                "obj:test",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == 0, r.output
        combined = (r.output + (r.stderr or "")).lower()
        assert "object: obj:test" in combined
        assert "submitted 1 digest" in combined
        assert "phase: drafting -> content-pending" in combined
        assert "INFO " not in r.output

        carry_d = hashlib.sha256(b"carry-over").hexdigest()
        sd = _staging_dir(sha256_root_with_v1, staging, "obj:test")
        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_PENDING
        assert state.pending_digests == (carry_d,)
        assert [s.digests for s in state.submissions] == [(carry_d,)]
        assert [s.digests for s in sp_fake._submissions] == [(carry_d,)]


def _seed_sha256(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _seed_many(n: int) -> list[str]:
    return [_seed_sha256(f"d-{i}") for i in range(n)]


def _seed_inventory_with_digests(staging_dir: Path, digests: list[str]) -> None:
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
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:big",
                "--expect",
                "content",
            ),
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


class TestContentSubmissionPrimitives:
    def test_prepare_persists_intent_before_submit_posts(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        runtime,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"

        r = runner.invoke(
            app,
            _stage_args(empty_sha256_root, staging, "new", "obj:primitive", "--ots"),
        )
        assert r.exit_code == 0, r.output
        f = tmp_path / "primitive.txt"
        f.write_text("primitive")
        r = runner.invoke(
            app,
            _stage_args(empty_sha256_root, staging, "add", "obj:primitive", str(f)),
        )
        assert r.exit_code == 0, r.output

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
        sd = _staging_dir(empty_sha256_root, staging, "obj:primitive")

        run = wf_timestamp.TimestampRun(
            cfg=cfg,
            store=store,
            sr=sr,
            root_path=str(empty_sha256_root.resolve()),
            object_id="obj:primitive",
            staging_dir=sd,
            sp_factory=lambda cfg: sp_fake,
            btc_factory=lambda cfg: FakeBitcoinRPC(),
            runtime=runtime,
        )
        prepared = wf_timestamp.prepare_submission_locked(
            run.area, category_config=641, runtime=runtime
        )

        assert prepared.digests == (hashlib.sha256(b"primitive").hexdigest(),)
        assert sp_fake._submissions == []
        state = load_ots(sd)
        assert state.phase is Phase.DRAFTING
        assert state.pending_digests == prepared.digests
        assert len(state.submissions) == 1
        assert state.submissions[0].status == "submitting"

        result = wf_timestamp.submit_prepared_locked(run, prepared)

        assert result.submitted_count == 1
        assert len(sp_fake._submissions) == 1
        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_PENDING
        assert state.submissions[0].status == "submitted"


def _seed_submitting_intent(staging_dir: Path, digests: tuple[str, ...]) -> None:
    state = load_ots(staging_dir)
    state = replace(
        state,
        phase=Phase.CONTENT_PENDING,
        pending_digests=digests,
        submissions=(
            Submission(
                submitted_at=utc_iso(SYSTEM_CLOCK.now()),
                digests=digests,
                status="submitting",
                target=TimestampTarget.CONTENT,
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
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:crash",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == 0, r.output

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

        _seed_submitting_intent(sd, digests)
        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:dup",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == 0, r.output
        round_1_count = len(sp_fake._submissions)
        assert round_1_count >= 1

        _seed_submitting_intent(sd, digests)
        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "timestamp",
                "obj:dup",
                "--expect",
                "content",
            ),
        )
        assert r.exit_code == 0, r.output

        assert len(sp_fake._submissions) > round_1_count
        for s in sp_fake._submissions:
            assert set(s.digests) == set(digests)

        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_PENDING
        assert all(s.status != "submitting" for s in state.submissions)


class TestCarriedProofReuse:
    def test_re_added_file_reuses_the_objects_committed_proof(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """v2 of an OTS object re-adds `a.txt`, whose digest v1 proves with
        `a.txt.ots`: nothing is submitted, the phase moves on at once and
        the companion maps to the committed proof's digest (ADR-0004)."""
        from test_stage_commit_ots import _setup_at_inventory_upgraded
        from test_stage_timestamp_inventory_upgrade import (
            _inject_fake_btc,
        )
        from test_stage_timestamp_inventory_upgrade import (
            _inject_fake_sp as _inject_sp,
        )
        from test_stage_timestamp_inventory_upgrade import (
            _write_config as _write_ots_config,
        )

        _write_ots_config(tmp_path, monkeypatch)
        _inject_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)
        staging = tmp_path / "staging"
        oid = "obj:carried"
        _setup_at_inventory_upgraded(
            runner, empty_sha256_root, staging, oid, sp_fake, btc_fake
        )
        r = runner.invoke(app, _stage_args(empty_sha256_root, staging, "commit", oid))
        assert r.exit_code == 0, r.output

        sr = StorageRoot.open(LocalFS(empty_sha256_root))
        v1_state = sr.get_object(oid).inventory.inventory.versions["v1"].state
        by_path = {p: d for d, ps in v1_state.items() for p in ps}
        committed_proof = by_path["a.txt.ots"]
        sent = len(sp_fake._submissions)

        r = runner.invoke(
            app, _stage_args(empty_sha256_root, staging, "new", oid, "--ots")
        )
        assert r.exit_code == 0, r.output
        src = tmp_path / "again" / "a.txt"
        src.parent.mkdir()
        src.write_bytes(b"alpha")
        r = runner.invoke(
            app, _stage_args(empty_sha256_root, staging, "add", oid, str(src))
        )
        assert r.exit_code == 0, r.output

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root, staging, "timestamp", oid, "--expect", "content"
            ),
        )
        assert r.exit_code == 0, r.output

        sd = _staging_dir(empty_sha256_root, staging, oid)
        assert len(sp_fake._submissions) == sent
        state = load_ots(sd)
        assert state.phase is Phase.CONTENT_UPGRADED
        assert state.submissions == ()
        inv = StagingStore(staging).load_inventory(
            str(empty_sha256_root.resolve()), oid
        )
        staged = {p: d for d, ps in inv.versions[inv.head].state.items() for p in ps}
        assert staged["a.txt"] == hashlib.sha256(b"alpha").hexdigest()
        assert staged["a.txt.ots"] == committed_proof
