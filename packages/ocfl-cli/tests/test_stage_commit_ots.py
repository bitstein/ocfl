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

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fakes import FakeSimpleProof
from msgspec.structs import replace

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
    _new_ots_staging,
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
from ocfl_cli.stage.fsm import Phase
from ocfl_cli.stage.schemas import StagedMetadata, StagedUser
from ocfl_cli.stage.state import (
    VerifiedProof,
    has_ots,
    load_ots,
    ots_cache_path,
    plan_path,
    save_ots,
)
from ocfl_cli.stage.store import StagingStore
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


def _setup_missing_metadata_inventory_upgraded(
    runner: CliRunner,
    root: Path,
    staging: Path,
    object_id: str,
    sp_fake: FakeSimpleProof,
    btc_fake,
) -> tuple[Path, bytes]:
    """Build an upgraded inventory plan that is intentionally missing message."""
    sd, _paths, digests = _new_ots_staging(
        runner, root, staging, object_id, ("a.txt", b"alpha")
    )
    store = StagingStore(staging)
    store.save_metadata(
        str(root.resolve()),
        object_id,
        StagedMetadata(
            user=StagedUser(name="Alice", address="mailto:alice@example.org")
        ),
    )
    da = digests["a.txt"]

    ots_src = staging.parent / f"{object_id.replace(':', '_')}.a.txt.ots"
    ots_src.write_bytes(b"\x00fake-ots-bytes-opaque-to-this-command")
    r = runner.invoke(app, _stage_args(root, staging, "add", object_id, str(ots_src)))
    assert r.exit_code == 0, r.output

    state = load_ots(sd)
    state = replace(
        state,
        phase=Phase.CONTENT_UPGRADED,
        pending_digests=(da,),
        content_proofs={
            da: VerifiedProof(
                ots_path=f"ots-cache/{da}.ots",
                verified=True,
                block_height=DIGEST_BLOCK_HEIGHT,
                confirmations_at_verify=MIN_CONFS,
                verified_at="2026-04-23T16:30:00+00:00",
            )
        },
    )
    save_ots(sd, state)

    r = runner.invoke(
        app,
        _stage_args(
            root,
            staging,
            "timestamp",
            "inventory",
            object_id,
            "--allow-missing-metadata",
        ),
    )
    assert r.exit_code == 0, r.output

    state = load_ots(sd)
    assert state.inventory_proof is not None
    inv_digest = state.inventory_proof.digest
    ots_bytes = _ots_for(inv_digest)
    _seed_inv_completed_verify_job(sp_fake, sd, inv_digest, ots_bytes)
    _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
    _register_attested(btc_fake.rpc, inv_digest)

    r = runner.invoke(
        app,
        _stage_args(root, staging, "timestamp", "inventory", object_id, "--upgrade"),
    )
    assert r.exit_code == 0, r.output
    assert load_ots(sd).phase is Phase.INVENTORY_UPGRADED
    return sd, ots_bytes


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
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _da, _inv_digest, ots_bytes = _setup_at_inventory_upgraded(
            runner, empty_sha256_root, staging, "obj:commit-ok", sp_fake, btc_fake
        )

        r = runner.invoke(
            app,
            _stage_args(empty_sha256_root, staging, "commit", "obj:commit-ok"),
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
        config_path = ext_path.parent / "config.json"
        assert json.loads(config_path.read_text()) == {
            "extensionName": "NNNN-ots-timestamps",
            "timestampInventory": True,
            "timestampContent": True,
        }

        # Staging purged (also removes the ots-cache subtree).
        store = StagingStore(staging)
        root_path = str(empty_sha256_root.resolve())
        assert not store.exists(root_path, "obj:commit-ok")
        assert not sd.exists()


class TestStageCommitOtsAfterMetadataInvalidation:
    def test_commit_blocks_after_metadata_edit_invalidates_inventory_plan(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _da, _inv_digest, _ots_bytes = _setup_at_inventory_upgraded(
            runner,
            empty_sha256_root,
            staging,
            "obj:commit-invalidated",
            sp_fake,
            btc_fake,
        )

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "metadata",
                "obj:commit-invalidated",
                "--message",
                "Updated metadata",
            ),
        )
        assert r.exit_code == 0, r.output
        assert load_ots(sd).phase is Phase.CONTENT_UPGRADED
        assert not plan_path(sd).exists()

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "commit",
                "obj:commit-invalidated",
            ),
        )

        assert r.exit_code == 3, r.output
        out = (r.output or "").lower()
        assert "stage timestamp inventory" in out, r.output
        assert "stage timestamp inventory --upgrade" in out, r.output

        store = StagingStore(staging)
        root_path = str(empty_sha256_root.resolve())
        assert store.exists(root_path, "obj:commit-invalidated")
        sr = StorageRoot(LocalFS(empty_sha256_root))
        assert not sr.get_object("obj:commit-invalidated").exists


class TestStageCommitOtsMetadataGate:
    def test_commit_blocks_when_timestamped_plan_is_missing_metadata(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        _setup_missing_metadata_inventory_upgraded(
            runner,
            empty_sha256_root,
            staging,
            "obj:commit-missing-metadata",
            sp_fake,
            btc_fake,
        )

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "commit",
                "obj:commit-missing-metadata",
            ),
        )

        assert r.exit_code == 1, r.output
        assert "timestamped inventory plan missing metadata: message" in (
            r.output.lower()
        )

    def test_commit_allows_missing_metadata_with_explicit_bypass(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        _sd, ots_bytes = _setup_missing_metadata_inventory_upgraded(
            runner,
            empty_sha256_root,
            staging,
            "obj:commit-missing-metadata-allowed",
            sp_fake,
            btc_fake,
        )

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "commit",
                "obj:commit-missing-metadata-allowed",
                "--allow-missing-metadata",
            ),
        )

        assert r.exit_code == 0, r.output
        sr = StorageRoot(LocalFS(empty_sha256_root))
        obj = sr.get_object("obj:commit-missing-metadata-allowed")
        assert obj.exists
        inv = obj.inventory.inventory
        version = inv.versions[inv.head]
        assert version.message is None
        assert version.user is not None
        assert version.user.name == "Alice"

        ext_path = (
            empty_sha256_root
            / "obj:commit-missing-metadata-allowed"
            / "extensions"
            / "NNNN-ots-timestamps"
            / "inventory.json.v1.ots"
        )
        assert ext_path.read_bytes() == ots_bytes


# =========================================================================== #
# Layer-2: corrupted ots-cache → AttestationMismatchError + actionable hint
# =========================================================================== #


class TestStageCommitOtsCorruptedCache:
    def test_commit_halts_when_plan_digest_does_not_match_verified_digest(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _da, inv_digest, _ots_bytes = _setup_at_inventory_upgraded(
            runner,
            empty_sha256_root,
            staging,
            "obj:commit-plan-mismatch",
            sp_fake,
            btc_fake,
        )
        other_sd, _other_da, other_inv_digest, _other_ots_bytes = (
            _setup_at_inventory_upgraded(
                runner,
                empty_sha256_root,
                staging,
                "obj:commit-plan-other",
                sp_fake,
                btc_fake,
            )
        )
        assert other_inv_digest != inv_digest
        plan_path(sd).write_bytes(plan_path(other_sd).read_bytes())

        r = runner.invoke(
            app,
            _stage_args(
                empty_sha256_root,
                staging,
                "commit",
                "obj:commit-plan-mismatch",
                "--allow-missing-metadata",
            ),
        )

        assert r.exit_code == 5, r.output
        out = (r.output or "").lower()
        assert "does not match verified inventory digest" in out, r.output
        assert "stage timestamp inventory" in out, r.output
        assert "stage timestamp inventory --upgrade" in out, r.output

        store = StagingStore(staging)
        root_path = str(empty_sha256_root.resolve())
        assert store.exists(root_path, "obj:commit-plan-mismatch")
        sr = StorageRoot(LocalFS(empty_sha256_root))
        assert not sr.get_object("obj:commit-plan-mismatch").exists

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
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

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
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

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
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        _sd, _da, _inv_digest, _ots_bytes = _setup_at_inventory_upgraded(
            runner, empty_sha256_root, staging, "obj:commit-sigint", sp_fake, btc_fake
        )

        with patch(
            "ocfl_cli.stage.service.execute_plan", side_effect=KeyboardInterrupt
        ):
            r = runner.invoke(
                app,
                _stage_args(
                    empty_sha256_root,
                    staging,
                    "commit",
                    "obj:commit-sigint",
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
        _inject_fake_sp(runner, sp_fake)
        _inject_fake_btc(runner, btc_fake.rpc)

        staging = tmp_path / "staging"
        sd, _da, inv_digest, ots_bytes = _setup_at_inventory_upgraded(
            runner, empty_sha256_root, staging, "obj:commit-crash", sp_fake, btc_fake
        )

        # --- Round 1: induced crash ---
        with patch(
            "ocfl_cli.stage.service.execute_plan",
            side_effect=RuntimeError("simulated crash"),
        ):
            r1 = runner.invoke(
                app,
                _stage_args(
                    empty_sha256_root,
                    staging,
                    "commit",
                    "obj:commit-crash",
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
            _stage_args(empty_sha256_root, staging, "commit", "obj:commit-crash"),
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

        runner.invoke(
            app,
            _stage_args(
                storage_root,
                staging,
                "new",
                "obj:committed",
                "-m",
                "first version",
                "--name",
                "Alice",
                "--address",
                "mailto:alice@example.org",
            ),
        )
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
