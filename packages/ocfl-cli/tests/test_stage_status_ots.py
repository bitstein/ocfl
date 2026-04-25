"""Layer-2 tests for ``ocfl stage status`` OTS extension (step 17).

Pins the contract from ``OTS workflow contract::stage_status``:

- Dense human output (``Phase:``, ``Bitcoin:``, ``Next:`` lines + per-job
  outstanding lines with humanized "ago" text).
- ``--json`` emits ``schema_version=1`` per the pinned schema with keys:
  ``schema_version``, ``extension``, ``phase``, ``counts`` (5 keys),
  ``outstanding_jobs``, ``bitcoin``, ``simpleproof``, ``cache_bytes``,
  ``next_action_kind``, ``next_action``.
- ``next_action_kind`` is the stable enum
  ``{timestamp_content, upgrade_content, retry_content,
     timestamp_inventory, upgrade_inventory, retry_inventory,
     commit, add_content, none}``; scripts MUST tolerate unknown values.
- No lock acquired (reads ``ots.json`` directly).
- Non-OTS fallback: ``ots.json`` absent ⇒ existing pre-OTS diff output
  unchanged; ``--json`` wraps as ``{"extension": null, "changes": [...]}``;
  OTS fields are omitted entirely.
- ``bitcoin.in_sync`` derivation:
  ``(not initial_block_download) and verification_progress >= 0.9999``.
- ``cache_bytes`` is the byte sum of files in ``<staging>/ots-cache/``;
  ``0`` when the dir is absent.

RED until ``stage status`` learns the OTS shape.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from fakes import FakeBitcoinRPC, FakeSimpleProof
from typer.testing import CliRunner

from ocfl_cli.main import app
from ocfl_cli.staging import (
    InventoryProof,
    Phase,
    StagingStore,
    Submission,
    VerifiedProof,
    VerifyJob,
    load_ots,
    save_ots,
)
from ocfl_lib import LocalFS, Spec, StorageRoot, get_layout

# --------------------------------------------------------------------------- #
# Config + path helpers (mirrors test_stage_timestamp_inventory.py).
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
rpc_cookie = "~/.bitcoin/.cookie"
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
    # chmod 600 so the loose-perms WARNING (which lands on stderr and gets
    # merged into Click's mixed-stream output) doesn't pollute the JSON
    # captured by `r.output`.
    cfg.chmod(0o600)
    monkeypatch.setenv("OCFL_CONFIG", str(cfg))
    return cfg


def _stage_args(root: Path, staging: Path, *args: str) -> list[str]:
    return ["--root", str(root), "stage", "--staging-root", str(staging), *args]


def _stage_args_json(root: Path, staging: Path, *args: str) -> list[str]:
    """``--json`` is a top-level (app-level) flag, before the ``stage`` group."""
    return [
        "--root",
        str(root),
        "--json",
        "stage",
        "--staging-root",
        str(staging),
        *args,
    ]


def _staging_dir(root: Path, staging: Path, object_id: str) -> Path:
    store = StagingStore(staging)
    return store.staging_path(str(root.resolve()), object_id)


def _inject_fake_sp(monkeypatch: pytest.MonkeyPatch, fake: FakeSimpleProof) -> None:
    from ocfl_cli.commands import stage as stage_cmd

    monkeypatch.setattr(
        stage_cmd, "_get_sp_client", lambda *a, **kw: fake, raising=False
    )


def _inject_fake_btc(monkeypatch: pytest.MonkeyPatch, fake: FakeBitcoinRPC) -> None:
    from ocfl_cli.commands import stage as stage_cmd

    monkeypatch.setattr(stage_cmd, "_get_btc_rpc", lambda *a, **kw: fake, raising=False)


# --------------------------------------------------------------------------- #
# Storage-root + staging fixtures (replicated locally per established
# convention; see test_stage_timestamp_inventory.py).
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
# Setup helpers: bring an OTS staging into each phase by mutating ots.json
# directly. The status command READS ots.json without enforcing the FSM
# (status itself is `_STAY` in the transition matrix), so direct mutation
# is sufficient and avoids dragging in unrelated workflow scaffolding.
# --------------------------------------------------------------------------- #


def _new_ots_staging_no_files(
    runner: CliRunner,
    root: Path,
    staging: Path,
    object_id: str,
) -> Path:
    """Create a fresh OTS staging at phase=drafting with no files added."""
    r = runner.invoke(app, _stage_args(root, staging, "new", object_id, "--ots"))
    assert r.exit_code == 0, r.output
    return _staging_dir(root, staging, object_id)


def _new_ots_staging(
    runner: CliRunner,
    root: Path,
    staging: Path,
    object_id: str,
    *files: tuple[str, bytes],
) -> tuple[Path, dict[str, str]]:
    """Create a fresh OTS staging at phase=drafting with *files* added."""
    sd = _new_ots_staging_no_files(runner, root, staging, object_id)
    digests: dict[str, str] = {}
    for name, data in files:
        src = staging.parent / name
        src.write_bytes(data)
        r = runner.invoke(app, _stage_args(root, staging, "add", object_id, str(src)))
        assert r.exit_code == 0, r.output
        digests[name] = hashlib.sha256(data).hexdigest()
    return sd, digests


def _set_phase(
    sd: Path,
    phase: Phase,
    *,
    pending_digests: tuple[str, ...] = (),
    submissions: tuple[Submission, ...] = (),
    verify_jobs: tuple[VerifyJob, ...] = (),
    content_proofs: dict[str, VerifiedProof] | None = None,
    inventory_proof: InventoryProof | None = None,
) -> None:
    state = load_ots(sd)
    state = replace(
        state,
        phase=phase,
        pending_digests=pending_digests,
        submissions=submissions,
        verify_jobs=verify_jobs,
        content_proofs=content_proofs if content_proofs is not None else {},
        inventory_proof=inventory_proof,
    )
    save_ots(sd, state)


def _verified_content_proof(digest: str) -> VerifiedProof:
    return VerifiedProof(
        ots_path=f"ots-cache/{digest}.ots",
        verified=True,
        block_height=800_000,
        confirmations_at_verify=3,
        verified_at="2026-04-23T16:30:00+00:00",
    )


def _verified_inv_proof(version: str, inv_digest: str) -> InventoryProof:
    return InventoryProof(
        version=version,
        digest=inv_digest,
        proof=_verified_content_proof(inv_digest),
        job_id=None,
    )


# =========================================================================== #
# Layer-2: OTS phase x next_action_kind matrix
# =========================================================================== #


class TestStageStatusOts:
    # ---- 1. drafting, no files → add_content ----
    def test_status_drafting_no_files_next_action_add_content(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(monkeypatch, sp_fake)
        _inject_fake_btc(monkeypatch, btc_fake.rpc)

        staging = tmp_path / "staging"
        _new_ots_staging_no_files(runner, empty_sha256_root, staging, "obj:s-empty")

        r = runner.invoke(
            app, _stage_args_json(empty_sha256_root, staging, "status", "obj:s-empty")
        )
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        assert data["phase"] == Phase.DRAFTING.value
        assert data["next_action_kind"] == "add_content"
        assert data["counts"]["total"] == 0

    # ---- 2. drafting, with files → timestamp_content ----
    def test_status_drafting_with_files_next_action_timestamp_content(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(monkeypatch, sp_fake)
        _inject_fake_btc(monkeypatch, btc_fake.rpc)

        staging = tmp_path / "staging"
        _new_ots_staging(
            runner, empty_sha256_root, staging, "obj:s-draft", ("a.txt", b"alpha")
        )

        r = runner.invoke(
            app, _stage_args_json(empty_sha256_root, staging, "status", "obj:s-draft")
        )
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        assert data["phase"] == Phase.DRAFTING.value
        assert data["next_action_kind"] == "timestamp_content"
        assert data["counts"]["total"] == 1

    # ---- 3. content-pending, in-flight VerifyJob → upgrade_content ----
    def test_status_content_pending_outstanding_jobs_listed(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(monkeypatch, sp_fake)
        _inject_fake_btc(monkeypatch, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, digests = _new_ots_staging(
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
                    job_id="job-1",
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
        assert r.exit_code == 0, r.output
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
        assert isinstance(entry["submitted_at"], str)
        assert entry["submitted_at"]  # non-empty ISO-8601
        assert isinstance(entry["age_seconds"], int)
        assert entry["age_seconds"] >= 0

    # ---- 4. content-pending + FAILED job covering remaining digest -> retry_content
    def test_status_content_pending_with_failed_job_next_retry(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(monkeypatch, sp_fake)
        _inject_fake_btc(monkeypatch, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, digests = _new_ots_staging(
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
                    job_id="job-fail",
                    attempts=1,
                ),
            ),
            verify_jobs=(VerifyJob(job_id="job-fail", digests=(da,), status="FAILED"),),
        )

        r = runner.invoke(
            app,
            _stage_args_json(empty_sha256_root, staging, "status", "obj:s-pend-fail"),
        )
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        assert data["next_action_kind"] == "retry_content"
        assert data["counts"]["failed"] >= 1

    # ---- 5. content-upgraded → timestamp_inventory ----
    def test_status_content_upgraded_next_timestamp_inventory(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(monkeypatch, sp_fake)
        _inject_fake_btc(monkeypatch, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, digests = _new_ots_staging(
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
            content_proofs={da: _verified_content_proof(da)},
        )

        r = runner.invoke(
            app, _stage_args_json(empty_sha256_root, staging, "status", "obj:s-cu")
        )
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        assert data["next_action_kind"] == "timestamp_inventory"
        assert data["counts"]["upgraded"] == 1
        assert data["counts"]["pending"] == 0

    # ---- 6. inventory-pending → upgrade_inventory ----
    def test_status_inventory_pending_next_upgrade_inventory(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(monkeypatch, sp_fake)
        _inject_fake_btc(monkeypatch, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, digests = _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:s-ip",
            ("a.txt", b"alpha"),
        )
        da = digests["a.txt"]
        inv_d = "f" * 64
        unverified_inv = InventoryProof(
            version="v1",
            digest=inv_d,
            proof=VerifiedProof(verified=False),
            job_id="job-inv",
        )
        _set_phase(
            sd,
            Phase.INVENTORY_PENDING,
            pending_digests=(da,),
            content_proofs={da: _verified_content_proof(da)},
            verify_jobs=(
                VerifyJob(job_id="job-inv", digests=(inv_d,), status="IN_PROGRESS"),
            ),
            inventory_proof=unverified_inv,
        )

        r = runner.invoke(
            app, _stage_args_json(empty_sha256_root, staging, "status", "obj:s-ip")
        )
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        assert data["next_action_kind"] == "upgrade_inventory"

    # ---- 7. inventory-pending with FAILED inv job → retry_inventory ----
    def test_status_inventory_pending_with_failed_inv_job_next_retry(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(monkeypatch, sp_fake)
        _inject_fake_btc(monkeypatch, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, digests = _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:s-ip-fail",
            ("a.txt", b"alpha"),
        )
        da = digests["a.txt"]
        inv_d = "e" * 64
        unverified_inv = InventoryProof(
            version="v1",
            digest=inv_d,
            proof=VerifiedProof(verified=False),
            job_id="job-inv-fail",
        )
        _set_phase(
            sd,
            Phase.INVENTORY_PENDING,
            pending_digests=(da,),
            content_proofs={da: _verified_content_proof(da)},
            verify_jobs=(
                VerifyJob(job_id="job-inv-fail", digests=(inv_d,), status="FAILED"),
            ),
            inventory_proof=unverified_inv,
        )

        r = runner.invoke(
            app,
            _stage_args_json(empty_sha256_root, staging, "status", "obj:s-ip-fail"),
        )
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        assert data["next_action_kind"] == "retry_inventory"

    # ---- 8. inventory-upgraded → commit ----
    def test_status_inventory_upgraded_next_commit(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(monkeypatch, sp_fake)
        _inject_fake_btc(monkeypatch, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, digests = _new_ots_staging(
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
            content_proofs={da: _verified_content_proof(da)},
            inventory_proof=_verified_inv_proof("v1", inv_d),
        )

        r = runner.invoke(
            app, _stage_args_json(empty_sha256_root, staging, "status", "obj:s-iu")
        )
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        assert data["next_action_kind"] == "commit"

    # ---- 9. committed → none ----
    def test_status_committed_next_none(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(monkeypatch, sp_fake)
        _inject_fake_btc(monkeypatch, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, digests = _new_ots_staging(
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
            content_proofs={da: _verified_content_proof(da)},
            inventory_proof=_verified_inv_proof("v1", inv_d),
        )

        r = runner.invoke(
            app, _stage_args_json(empty_sha256_root, staging, "status", "obj:s-cm")
        )
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        assert data["next_action_kind"] == "none"

    # ---- 10. JSON schema v1 invariants ----
    def test_status_json_schema_v1_invariants(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(monkeypatch, sp_fake)
        _inject_fake_btc(monkeypatch, btc_fake.rpc)

        staging = tmp_path / "staging"
        _new_ots_staging(
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
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        assert isinstance(data, dict)
        assert data["schema_version"] == 1
        assert data["extension"] == "NNNN-ots-timestamps"
        counts = data["counts"]
        assert isinstance(counts, dict)
        for key in ("total", "submitted", "upgraded", "pending", "failed"):
            assert key in counts, key
            assert isinstance(counts[key], int)

    # ---- 11. cache_bytes sums ots-cache files ----
    def test_status_cache_bytes_sums_ots_cache_files(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(monkeypatch, sp_fake)
        _inject_fake_btc(monkeypatch, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _ = _new_ots_staging(
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
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        assert data["cache_bytes"] == sum(sizes)

    # ---- 12. cache_bytes==0 when no ots-cache dir ----
    def test_status_cache_bytes_zero_when_no_cache_dir(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(monkeypatch, sp_fake)
        _inject_fake_btc(monkeypatch, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _ = _new_ots_staging(
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
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        assert data["cache_bytes"] == 0

    # ---- 13. no lock acquired (with_lock guarded against being called) ----
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
        correct semantics for an inspector). Monkeypatch
        ``with_lock`` to raise if called from anywhere in the status path.
        """
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(monkeypatch, sp_fake)
        _inject_fake_btc(monkeypatch, btc_fake.rpc)

        from ocfl_cli import staging as staging_mod
        from ocfl_cli.commands import stage as stage_cmd

        def _boom(*a: object, **kw: object):
            raise AssertionError("status MUST NOT take the staging .lock")

        monkeypatch.setattr(staging_mod, "with_lock", _boom, raising=False)
        monkeypatch.setattr(stage_cmd, "with_lock", _boom, raising=False)

        staging = tmp_path / "staging"
        sd, _ = _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:s-nolock",
            ("a.txt", b"alpha"),
        )

        r = runner.invoke(
            app,
            _stage_args_json(empty_sha256_root, staging, "status", "obj:s-nolock"),
        )
        assert r.exit_code == 0, r.output
        # As an additional belt-and-braces check, no .lock file should exist
        # after a status invocation (with_lock would have created one).
        assert not (sd / ".lock").exists()

    # ---- 14. Human output is dense (Phase / Bitcoin / Next + per-job line) ----
    def test_status_human_output_dense(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(monkeypatch, sp_fake)
        _inject_fake_btc(monkeypatch, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, digests = _new_ots_staging(
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
                    job_id="job-h",
                    attempts=1,
                ),
            ),
            verify_jobs=(
                VerifyJob(job_id="job-h", digests=(da,), status="IN_PROGRESS"),
            ),
        )

        r = runner.invoke(
            app, _stage_args(empty_sha256_root, staging, "status", "obj:s-human")
        )
        assert r.exit_code == 0, r.output
        out = r.output
        assert "Phase:" in out
        assert "Bitcoin:" in out
        assert "Next:" in out
        # Per-job line names the in-flight job and uses humanized "ago" text.
        assert "job-h" in out
        assert "ago" in out

    # ---- 15. humanize age: recent seconds ----
    def test_status_humanize_age_recent_seconds(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Ages under a minute render as ``Ns ago`` (or close-equivalent)."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(monkeypatch, sp_fake)
        _inject_fake_btc(monkeypatch, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, digests = _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:s-h-sec",
            ("a.txt", b"alpha"),
        )
        da = digests["a.txt"]

        # Pin "now" inside the status command so age math is deterministic.
        from datetime import UTC, datetime, timedelta

        fake_now = datetime(2026, 4, 25, 14, 0, 30, tzinfo=UTC)
        submitted = fake_now - timedelta(seconds=30)
        submitted_iso = submitted.isoformat(timespec="seconds")

        from ocfl_cli.commands import stage as stage_cmd

        monkeypatch.setattr(stage_cmd, "_status_now", lambda: fake_now, raising=False)

        _set_phase(
            sd,
            Phase.CONTENT_PENDING,
            pending_digests=(da,),
            submissions=(
                Submission(
                    submitted_at=submitted_iso,
                    digests=(da,),
                    status="submitted",
                    job_id="job-sec",
                    attempts=1,
                ),
            ),
            verify_jobs=(
                VerifyJob(job_id="job-sec", digests=(da,), status="IN_PROGRESS"),
            ),
        )

        r = runner.invoke(
            app, _stage_args(empty_sha256_root, staging, "status", "obj:s-h-sec")
        )
        assert r.exit_code == 0, r.output
        assert "30s ago" in r.output

    # ---- 16. humanize age: hours ----
    def test_status_humanize_age_hours(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(monkeypatch, sp_fake)
        _inject_fake_btc(monkeypatch, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, digests = _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:s-h-hr",
            ("a.txt", b"alpha"),
        )
        da = digests["a.txt"]

        from datetime import UTC, datetime, timedelta

        fake_now = datetime(2026, 4, 25, 14, 0, 0, tzinfo=UTC)
        submitted = fake_now - timedelta(seconds=7200)
        submitted_iso = submitted.isoformat(timespec="seconds")

        from ocfl_cli.commands import stage as stage_cmd

        monkeypatch.setattr(stage_cmd, "_status_now", lambda: fake_now, raising=False)

        _set_phase(
            sd,
            Phase.CONTENT_PENDING,
            pending_digests=(da,),
            submissions=(
                Submission(
                    submitted_at=submitted_iso,
                    digests=(da,),
                    status="submitted",
                    job_id="job-hr",
                    attempts=1,
                ),
            ),
            verify_jobs=(
                VerifyJob(job_id="job-hr", digests=(da,), status="IN_PROGRESS"),
            ),
        )

        r = runner.invoke(
            app, _stage_args(empty_sha256_root, staging, "status", "obj:s-h-hr")
        )
        assert r.exit_code == 0, r.output
        assert "2h ago" in r.output

    # ---- 17. bitcoin.in_sync == True when node synced ----
    def test_status_bitcoin_in_sync_field_true_when_node_synced(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """FakeBitcoinRPC reports ibd=False, vp=1.0 ⇒ ``bitcoin.in_sync == True``."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(monkeypatch, sp_fake)
        _inject_fake_btc(monkeypatch, btc_fake.rpc)

        staging = tmp_path / "staging"
        _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:s-btc",
            ("a.txt", b"alpha"),
        )

        r = runner.invoke(
            app, _stage_args_json(empty_sha256_root, staging, "status", "obj:s-btc")
        )
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        bitcoin = data["bitcoin"]
        assert isinstance(bitcoin, dict)
        assert bitcoin["in_sync"] is True
        assert isinstance(bitcoin["height"], int)
        assert isinstance(bitcoin["tip_hash"], str)

    # ---- 18. bitcoin section null when no [bitcoin] config ----
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
        _write_config(tmp_path, monkeypatch)  # full config to enable `stage new --ots`
        _inject_fake_sp(monkeypatch, sp_fake)

        staging = tmp_path / "staging"
        _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:s-btc-null",
            ("a.txt", b"alpha"),
        )

        # Now drop [bitcoin] from config; [simpleproof] stays.
        _write_config(tmp_path, monkeypatch, body=_SP_OK)

        r = runner.invoke(
            app,
            _stage_args_json(empty_sha256_root, staging, "status", "obj:s-btc-null"),
        )
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        assert data["bitcoin"] is None

    # ---- 19. simpleproof.configured reflects config presence ----
    def test_status_simpleproof_configured_reflects_config(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(monkeypatch, sp_fake)
        _inject_fake_btc(monkeypatch, btc_fake.rpc)

        staging = tmp_path / "staging"
        _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:s-sp",
            ("a.txt", b"alpha"),
        )

        r = runner.invoke(
            app, _stage_args_json(empty_sha256_root, staging, "status", "obj:s-sp")
        )
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        sp_section = data["simpleproof"]
        assert isinstance(sp_section, dict)
        assert sp_section["configured"] is True
        assert sp_section["category_id"] == 641

    # ---- 20. confirmations_required surfaces from config ----
    def test_status_confirmations_required_from_config(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(monkeypatch, sp_fake)
        _inject_fake_btc(monkeypatch, btc_fake.rpc)

        staging = tmp_path / "staging"
        _new_ots_staging(
            runner,
            empty_sha256_root,
            staging,
            "obj:s-conf",
            ("a.txt", b"alpha"),
        )

        r = runner.invoke(
            app, _stage_args_json(empty_sha256_root, staging, "status", "obj:s-conf")
        )
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        assert data["bitcoin"]["confirmations_required"] == 3


# =========================================================================== #
# Layer-2: Non-OTS fallback (ots.json absent)
# =========================================================================== #


class TestStageStatusNonOtsFallback:
    def test_status_non_ots_text_unchanged(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No ``ots.json`` ⇒ existing pre-OTS diff output preserved."""
        _write_config(tmp_path, monkeypatch)

        staging = tmp_path / "staging"
        r = runner.invoke(
            app, _stage_args(empty_sha256_root, staging, "new", "obj:non-ots")
        )
        assert r.exit_code == 0, r.output

        src = tmp_path / "src.txt"
        src.write_text("hello")
        r = runner.invoke(
            app,
            _stage_args(empty_sha256_root, staging, "add", "obj:non-ots", str(src)),
        )
        assert r.exit_code == 0, r.output

        r = runner.invoke(
            app, _stage_args(empty_sha256_root, staging, "status", "obj:non-ots")
        )
        assert r.exit_code == 0, r.output
        # Pre-OTS diff output: each line is "<sym> <path>" with the file present.
        assert "src.txt" in r.output

    def test_status_non_ots_json_wraps_changes_with_extension_null(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No ``ots.json`` ⇒ ``--json`` is ``{"extension": null, "changes": [...]}``.

        OTS-only fields (``schema_version``, ``phase``, ``counts``, etc.)
        MUST be omitted entirely so consumers can branch on
        ``extension`` presence.
        """
        _write_config(tmp_path, monkeypatch)

        staging = tmp_path / "staging"
        r = runner.invoke(
            app, _stage_args(empty_sha256_root, staging, "new", "obj:non-ots-json")
        )
        assert r.exit_code == 0, r.output
        src = tmp_path / "src.txt"
        src.write_text("hello")
        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root, staging, "add", "obj:non-ots-json", str(src)
            ),
        )
        assert r.exit_code == 0, r.output

        r = runner.invoke(
            app,
            _stage_args_json(empty_sha256_root, staging, "status", "obj:non-ots-json"),
        )
        assert r.exit_code == 0, r.output
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
        # Each change carries the same path/action shape as the prior impl.
        for entry in data["changes"]:
            assert "path" in entry
            assert "action" in entry
