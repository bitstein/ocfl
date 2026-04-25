"""Tests for `ocfl stage commit` OTS path (step 16 of `OTS workflow contract`).

Pins the deferred-`extension_files` completion at commit time:

- Precondition: phase ``inventory-upgraded``.
- Load ``<staging>/plan.json``, read the cached
  ``<staging>/ots-cache/<inv_digest>.ots`` bytes, re-parse, and assert
  ``parsed.commitment_digest == plan.new_inv.digest``. On mismatch /
  parse / missing → ``AttestationMismatchError`` with an actionable hint
  to re-run ``stage timestamp inventory --upgrade`` (HALT, exit 5).
- Fill ``plan.extension_files`` at
  ``extensions/NNNN-ots-timestamps/inventory.json.v<head>.ots`` with the
  cached bytes BEFORE ``prepare_steps``, so the extension-file step is
  included in the executable plan.
- Run ``prepare_steps`` + ``execute_plan``; on ``KeyboardInterrupt`` →
  ``revert_plan`` (exit 130) leaving staging intact for retry. On
  generic ``Exception`` → revert + exit 1; staging preserved; phase
  unchanged in ``ots.json``; ``plan.json`` and ``ots-cache/`` remain
  intact, so a fresh re-run of ``stage commit`` re-loads the same
  plan + cache and converges.
- On success, purge staging (which also removes ``ots-cache/`` since
  it lives inside the staging directory).

These tests are RED until the OTS commit path lands. Expected
diagnostics include "no extension file written" / "exit 0 vs exit 5"
mismatches, missing ``AttestationMismatchError`` import paths, or
KeyError-style failures probing for the cached extension bytes in the
committed object directory.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fakes import FakeSimpleProof

# Re-use the inventory-upgrade test harness verbatim instead of
# duplicating its setup helpers — that file already documents them as
# the canonical "get to phase=INVENTORY_UPGRADED with cached ots bytes"
# scaffolding (see test_stage_timestamp_inventory_upgrade.py docstring).
from test_stage_timestamp_inventory_upgrade import (
    DIGEST_BLOCK_HEIGHT,
    MIN_CONFS,
    _grow_chain_to,
    _inject_fake_btc,
    _inject_fake_sp,
    _ots_for,
    _register_attested,
    _seed_inv_completed_verify_job,
    _setup_at_inventory_pending,
    _stage_args,
    _staging_dir,
    _write_config,
)
from typer.testing import CliRunner

from ocfl_cli.main import app
from ocfl_cli.staging import (
    Phase,
    StagingStore,
    has_ots,
    load_ots,
    ots_cache_path,
    plan_path,
)
from ocfl_lib import LocalFS, Spec, StorageRoot, get_layout
from tests.helpers.ots import build_attestation

# --------------------------------------------------------------------------- #
# Local fixture (sibling tests redefine `empty_sha256_root` because it
# is not in conftest; replicate here per the established convention).
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
# Helper: compose the inventory-pending setup + the `--upgrade` pass to
# land at phase=INVENTORY_UPGRADED with `ots-cache/<inv_digest>.ots`
# populated. This is the precondition the OTS commit path expects.
# --------------------------------------------------------------------------- #


def _setup_at_inventory_upgraded(
    runner: CliRunner,
    root: Path,
    staging: Path,
    object_id: str,
    sp_fake: FakeSimpleProof,
    btc_fake,  # BtcFake from conftest — has .rpc + .stream
) -> tuple[Path, str, str, bytes]:
    """Returns ``(staging_dir, content_digest, inv_digest, cached_ots_bytes)``.

    Caller MUST have already wired ``_inject_fake_sp`` + ``_inject_fake_btc``.
    """
    sd, da, inv_digest = _setup_at_inventory_pending(runner, root, staging, object_id)
    ots_bytes = _ots_for(inv_digest)
    _seed_inv_completed_verify_job(sp_fake, sd, inv_digest, ots_bytes)
    _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
    _register_attested(btc_fake.rpc, inv_digest)

    r = runner.invoke(
        app,
        _stage_args(root, staging, "timestamp", "inventory", object_id, "--upgrade"),
    )
    assert r.exit_code == 0, r.output
    state = load_ots(sd)
    assert state.phase is Phase.INVENTORY_UPGRADED
    assert ots_cache_path(sd, inv_digest).read_bytes() == ots_bytes
    return sd, da, inv_digest, ots_bytes


# An arbitrary 32-byte SHA-256-shaped hex digest, distinct from anything
# the helpers produce, used to construct a "wrong-commitment" cache file.
_OTHER_64HEX = "a" * 64


# =========================================================================== #
# Layer-2: full commit happy path
# =========================================================================== #


class TestStageCommitOtsHappyPath:
    def test_inventory_upgraded_commit_writes_extension_and_purges_staging(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """At phase=INVENTORY_UPGRADED, ``stage commit``:

        - Loads plan.json, fills ``extension_files`` from cached ots
          bytes, and writes them at
          ``extensions/NNNN-ots-timestamps/inventory.json.v<head>.ots``.
        - Promotes the staged inventory to a real OCFL object (head=v1).
        - Purges the entire staging directory (incl. ``ots-cache/``).
        """
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(monkeypatch, sp_fake)
        _inject_fake_btc(monkeypatch, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _da, _inv_digest, ots_bytes = _setup_at_inventory_upgraded(
            runner, empty_sha256_root, staging, "obj:commit-ok", sp_fake, btc_fake
        )

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root, staging, "commit", "obj:commit-ok", "-m", "v1"
            ),
        )
        assert r.exit_code == 0, r.output

        # Storage object exists at v1.
        sr = StorageRoot(LocalFS(empty_sha256_root))
        obj = sr.get_object("obj:commit-ok")
        assert obj.exists
        assert obj.head.number == 1

        # Extension file landed in the OBJECT directory with cached bytes.
        ext_path = (
            empty_sha256_root
            / "obj:commit-ok"
            / "extensions"
            / "NNNN-ots-timestamps"
            / "inventory.json.v1.ots"
        )
        assert ext_path.is_file(), f"missing extension file at {ext_path}"
        assert ext_path.read_bytes() == ots_bytes

        # Staging purged (also removes the ots-cache subtree).
        store = StagingStore(staging)
        root_path = str(empty_sha256_root.resolve())
        assert not store.exists(root_path, "obj:commit-ok")
        assert not sd.exists()


# =========================================================================== #
# Layer-2: corrupted ots-cache → AttestationMismatchError + actionable hint
# =========================================================================== #


class TestStageCommitOtsCorruptedCache:
    def test_commit_halts_with_re_run_hint_on_commitment_mismatch(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Overwrite the cached ots with bytes that commit to a DIFFERENT
        digest than ``plan.new_inv.digest`` ⇒ HALT (exit 5) with hint."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(monkeypatch, sp_fake)
        _inject_fake_btc(monkeypatch, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _da, inv_digest, _ots_bytes = _setup_at_inventory_upgraded(
            runner, empty_sha256_root, staging, "obj:commit-corrupt", sp_fake, btc_fake
        )

        # Replace cache with an attestation over a DIFFERENT commitment.
        bad_bytes = build_attestation(
            bytes.fromhex(_OTHER_64HEX), block_height=DIGEST_BLOCK_HEIGHT
        )
        ots_cache_path(sd, inv_digest).write_bytes(bad_bytes)

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "commit",
                "obj:commit-corrupt",
                "-m",
                "v1",
            ),
        )
        assert r.exit_code == 5, r.output
        out = (r.output or "").lower()
        assert "re-run" in out, r.output
        assert "stage timestamp inventory --upgrade" in out, r.output

        # Staging preserved for the operator to act on the hint.
        store = StagingStore(staging)
        root_path = str(empty_sha256_root.resolve())
        assert store.exists(root_path, "obj:commit-corrupt")
        assert load_ots(sd).phase is Phase.INVENTORY_UPGRADED
        assert plan_path(sd).is_file()


# =========================================================================== #
# Layer-2: missing ots-cache → actionable hint to re-run upgrade
# =========================================================================== #


class TestStageCommitOtsMissingCache:
    def test_commit_halts_with_re_run_hint_when_cache_absent(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unlink ``ots-cache/<inv_digest>.ots`` ⇒ HALT (exit 5) with hint."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(monkeypatch, sp_fake)
        _inject_fake_btc(monkeypatch, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _da, inv_digest, _ots_bytes = _setup_at_inventory_upgraded(
            runner, empty_sha256_root, staging, "obj:commit-missing", sp_fake, btc_fake
        )

        ots_cache_path(sd, inv_digest).unlink()

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "commit",
                "obj:commit-missing",
                "-m",
                "v1",
            ),
        )
        assert r.exit_code == 5, r.output
        out = (r.output or "").lower()
        assert "re-run" in out, r.output
        assert "stage timestamp inventory --upgrade" in out, r.output

        # Staging preserved.
        store = StagingStore(staging)
        root_path = str(empty_sha256_root.resolve())
        assert store.exists(root_path, "obj:commit-missing")
        assert load_ots(sd).phase is Phase.INVENTORY_UPGRADED


# =========================================================================== #
# Layer-2: KeyboardInterrupt mid-execute → revert; staging preserved
# =========================================================================== #


class TestStageCommitOtsKeyboardInterrupt:
    def test_revert_removes_extension_file_and_preserves_staging(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``execute_plan`` raises ``KeyboardInterrupt`` ⇒ exit 130, the
        revert undoes any object-side writes (no extension file, no root
        inventory), and staging survives for retry."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(monkeypatch, sp_fake)
        _inject_fake_btc(monkeypatch, btc_fake.rpc)

        staging = tmp_path / "staging"
        _sd, _da, _inv_digest, _ots_bytes = _setup_at_inventory_upgraded(
            runner, empty_sha256_root, staging, "obj:commit-sigint", sp_fake, btc_fake
        )

        with patch(
            "ocfl_cli.commands.stage.execute_plan", side_effect=KeyboardInterrupt
        ):
            r = runner.invoke(
                app,
                _stage_args(
                    empty_sha256_root,
                    staging,
                    "commit",
                    "obj:commit-sigint",
                    "-m",
                    "v1",
                ),
            )

        assert r.exit_code == 130, r.output
        assert "interrupted" in (r.output or "").lower(), r.output

        # Staging preserved for retry.
        store = StagingStore(staging)
        root_path = str(empty_sha256_root.resolve())
        assert store.exists(root_path, "obj:commit-sigint")

        # No partial writes in the would-be object directory.
        obj_dir = empty_sha256_root / "obj:commit-sigint"
        ext_file = (
            obj_dir / "extensions" / "NNNN-ots-timestamps" / "inventory.json.v1.ots"
        )
        assert not ext_file.exists(), f"extension file leaked at {ext_file}"
        # No partial root inventory committed either.
        assert not (obj_dir / "inventory.json").exists()


# =========================================================================== #
# Layer-2: crash between load and execute → restart converges (idempotent)
# =========================================================================== #


class TestStageCommitOtsCrashRecovery:
    def test_generic_crash_then_retry_converges(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round 1: ``execute_plan`` raises a generic ``RuntimeError`` ⇒
        exit 1, staging preserved, phase unchanged in ots.json,
        ``plan.json`` + ``ots-cache/<inv_digest>.ots`` still on disk.

        Round 2: re-run ``stage commit`` (no patch) ⇒ load-parse-fill
        converges, exit 0, object exists, staging purged."""
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(monkeypatch, sp_fake)
        _inject_fake_btc(monkeypatch, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _da, inv_digest, ots_bytes = _setup_at_inventory_upgraded(
            runner, empty_sha256_root, staging, "obj:commit-crash", sp_fake, btc_fake
        )

        # --- Round 1: induced crash ---
        with patch(
            "ocfl_cli.commands.stage.execute_plan",
            side_effect=RuntimeError("simulated crash"),
        ):
            r1 = runner.invoke(
                app,
                _stage_args(
                    empty_sha256_root,
                    staging,
                    "commit",
                    "obj:commit-crash",
                    "-m",
                    "v1",
                ),
            )
        assert r1.exit_code == 1, r1.output

        # Staging + plan + cache all intact; phase un-advanced.
        store = StagingStore(staging)
        root_path = str(empty_sha256_root.resolve())
        assert store.exists(root_path, "obj:commit-crash")
        assert plan_path(sd).is_file()
        assert ots_cache_path(sd, inv_digest).read_bytes() == ots_bytes
        assert load_ots(sd).phase is Phase.INVENTORY_UPGRADED

        # --- Round 2: real run converges ---
        r2 = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root, staging, "commit", "obj:commit-crash", "-m", "v1"
            ),
        )
        assert r2.exit_code == 0, r2.output

        sr = StorageRoot(LocalFS(empty_sha256_root))
        obj = sr.get_object("obj:commit-crash")
        assert obj.exists
        assert obj.head.number == 1

        ext_path = (
            empty_sha256_root
            / "obj:commit-crash"
            / "extensions"
            / "NNNN-ots-timestamps"
            / "inventory.json.v1.ots"
        )
        assert ext_path.read_bytes() == ots_bytes

        assert not store.exists(root_path, "obj:commit-crash")
        assert not sd.exists()


# =========================================================================== #
# Layer-2: non-OTS regression — commit on a non-OTS staging is unchanged
# =========================================================================== #


class TestStageCommitNonOtsRegression:
    def test_full_workflow_unchanged_without_ots(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
    ) -> None:
        """Mirrors ``test_stage_commit.py::TestStageCommit::test_full_workflow``
        verbatim: no ``--ots``, no ``ots.json`` / ``plan.json`` written, the
        commit must take the existing pre-OTS path."""
        staging = tmp_path / "staging"
        src = tmp_path / "hello.txt"
        src.write_text("hello world")

        runner.invoke(app, _stage_args(storage_root, staging, "new", "obj:committed"))
        runner.invoke(
            app,
            _stage_args(storage_root, staging, "add", "obj:committed", str(src)),
        )

        # Sanity: the OTS extension is NOT active for this staging.
        sd = _staging_dir(storage_root, staging, "obj:committed")
        assert not has_ots(sd)
        assert not plan_path(sd).is_file()

        result = runner.invoke(
            app,
            _stage_args(
                storage_root,
                staging,
                "commit",
                "obj:committed",
                "-m",
                "first version",
            ),
        )
        assert result.exit_code == 0, result.output

        sr = StorageRoot(LocalFS(storage_root))
        obj = sr.get_object("obj:committed")
        assert obj.exists
        assert obj.head.number == 1

        # Staging purged.
        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        assert not store.exists(root_path, "obj:committed")

        # No OTS-extension dir created on the object (non-OTS path stays
        # off the extension-file branch entirely).
        assert not (
            storage_root / "obj:committed" / "extensions" / "NNNN-ots-timestamps"
        ).exists()
