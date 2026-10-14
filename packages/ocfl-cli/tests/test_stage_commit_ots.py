import hashlib
import json
from pathlib import Path

import pytest
from fakes import FakeBitcoinRPC, FakeSimpleProof
from fakes.fault_fs import raise_on_content_write
from fakes.locks import lock_runtime
from msgspec.structs import replace
from ots_helpers import (
    DIGEST_BLOCK_HEIGHT,
    MIN_CONFS,
    VERSION_METADATA,
    grow_chain_to,
    inject_fake_btc,
    inject_fake_sp,
    new_ots_staging,
    ots_for,
    register_attested,
    seed_inv_completed_verify_job,
    setup_at_inventory_upgraded,
    write_config,
)
from runner_helpers import injected, runner_obj, stage_args
from typer.testing import CliRunner

from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app
from ocfl_cli.model.metadata import StagedMetadata, StagedUser
from ocfl_cli.model.ots_state import Proof
from ocfl_cli.model.phase import Phase, TimestampTarget
from ocfl_cli.store import locks as locks
from ocfl_cli.store.ots_state import (
    load_ots,
    ots_cache_path,
    plan_path,
    save_ots,
)
from ocfl_cli.store.staging import StagingStore
from ocfl_lib import LocalFS, StorageRoot
from tests.helpers.ots import build_attestation


def _setup_missing_metadata_inventory_upgraded(
    runner: CliRunner,
    root: Path,
    staging: Path,
    object_id: str,
    sp_fake: FakeSimpleProof,
    btc_rpc: FakeBitcoinRPC,
) -> tuple[Path, bytes]:
    inject_fake_sp(runner, sp_fake)
    inject_fake_btc(runner, btc_rpc)
    sd, digests = new_ots_staging(
        runner,
        root,
        staging,
        object_id,
        ("a.txt", b"alpha"),
        new_args=VERSION_METADATA,
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

    # Staged as the content proof `a.txt.ots`: the commit refuses E902.
    ots_dir = staging.parent / f"{object_id.replace(':', '_')}-ots"
    ots_dir.mkdir()
    ots_src = ots_dir / "a.txt.ots"
    # A final proof of a.txt: the commit refuses an invalid or pending one.
    ots_src.write_bytes(
        build_attestation(
            hashlib.sha256(b"alpha").digest(), block_height=DIGEST_BLOCK_HEIGHT
        )
    )
    r = runner.invoke(app, stage_args(root, staging, "add", object_id, str(ots_src)))
    assert r.exit_code == ExitCode.SUCCESS, r.output

    state = load_ots(sd)
    state = replace(
        state,
        phase=Phase.CONTENT_UPGRADED,
        pending_digests=(da,),
        proofs={
            da: Proof(
                target=TimestampTarget.CONTENT,
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
        stage_args(
            root,
            staging,
            "timestamp",
            object_id,
            "--allow-missing-metadata",
            "--expect",
            "inventory",
        ),
    )
    assert r.exit_code == ExitCode.SUCCESS, r.output

    state = load_ots(sd)
    assert state.inventory is not None
    inv_digest = state.inventory.digest
    ots_bytes = ots_for(inv_digest)
    seed_inv_completed_verify_job(sp_fake, sd, inv_digest, ots_bytes)
    grow_chain_to(btc_rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
    register_attested(btc_rpc, inv_digest)

    r = runner.invoke(
        app,
        stage_args(root, staging, "upgrade", object_id, "--expect", "inventory"),
    )
    assert r.exit_code == ExitCode.SUCCESS, r.output
    assert load_ots(sd).phase is Phase.INVENTORY_UPGRADED
    return sd, ots_bytes


# An arbitrary 32-byte SHA-256-shaped hex digest, distinct from anything
# the helpers produce, used to construct a "wrong-commitment" cache file.
_OTHER_64HEX = "a" * 64


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
        write_config(tmp_path, monkeypatch)

        staging = tmp_path / "staging"
        sd, _da, _inv_digest, ots_bytes = setup_at_inventory_upgraded(
            runner, empty_sha256_root, staging, "obj:commit-ok", sp_fake, btc_fake.rpc
        )

        r = runner.invoke(
            app,
            stage_args(empty_sha256_root, staging, "commit", "obj:commit-ok"),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output

        sr = StorageRoot.open(LocalFS(empty_sha256_root))
        obj = sr.get_object("obj:commit-ok")
        assert obj.exists
        assert obj.head.number == 1

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
            "proofDigestAlgorithm": "sha256",
            "timestampInventory": True,
            "timestampContent": True,
        }

        store = StagingStore(staging)
        root_path = str(empty_sha256_root.resolve())
        assert not store.exists(root_path, "obj:commit-ok")
        assert not sd.exists()

    def test_commit_blocks_when_content_proof_invalidated_after_inventory_upgrade(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)

        staging = tmp_path / "staging"
        sd, da, _inv_digest, _ots_bytes = setup_at_inventory_upgraded(
            runner,
            empty_sha256_root,
            staging,
            "obj:commit-content-invalid",
            sp_fake,
            btc_fake.rpc,
        )
        state = load_ots(sd)
        state = replace(
            state,
            proofs={da: replace(state.proofs[da], verified=False)},
        )
        save_ots(sd, state)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "commit",
                "obj:commit-content-invalid",
            ),
        )

        assert r.exit_code == ExitCode.PHASE, r.output
        assert "content proof" in r.output.lower()
        assert "unverified" in r.output.lower()
        assert StagingStore(staging).exists(
            str(empty_sha256_root.resolve()), "obj:commit-content-invalid"
        )


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
        write_config(tmp_path, monkeypatch)

        staging = tmp_path / "staging"
        sd, _da, _inv_digest, _ots_bytes = setup_at_inventory_upgraded(
            runner,
            empty_sha256_root,
            staging,
            "obj:commit-invalidated",
            sp_fake,
            btc_fake.rpc,
        )

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "metadata",
                "obj:commit-invalidated",
                "--message",
                "Updated metadata",
            ),
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert load_ots(sd).phase is Phase.CONTENT_UPGRADED
        # ADR-0004: plan.json stays; the staged inventory is dropped.
        assert plan_path(sd).exists()
        assert load_ots(sd).inventory is None

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "commit",
                "obj:commit-invalidated",
            ),
        )

        assert r.exit_code == ExitCode.PHASE, r.output
        out = (r.output or "").lower()
        assert "next: ocfl stage timestamp obj:commit-invalidated" in out, r.output

        store = StagingStore(staging)
        root_path = str(empty_sha256_root.resolve())
        assert store.exists(root_path, "obj:commit-invalidated")
        sr = StorageRoot.open(LocalFS(empty_sha256_root))
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
        write_config(tmp_path, monkeypatch)

        staging = tmp_path / "staging"
        _setup_missing_metadata_inventory_upgraded(
            runner,
            empty_sha256_root,
            staging,
            "obj:commit-missing-metadata",
            sp_fake,
            btc_fake.rpc,
        )

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "commit",
                "obj:commit-missing-metadata",
            ),
        )

        assert r.exit_code == ExitCode.GENERIC, r.output
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
        write_config(tmp_path, monkeypatch)

        staging = tmp_path / "staging"
        _sd, ots_bytes = _setup_missing_metadata_inventory_upgraded(
            runner,
            empty_sha256_root,
            staging,
            "obj:commit-missing-metadata-allowed",
            sp_fake,
            btc_fake.rpc,
        )

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "commit",
                "obj:commit-missing-metadata-allowed",
                "--allow-missing-metadata",
            ),
        )

        assert r.exit_code == ExitCode.SUCCESS, r.output
        sr = StorageRoot.open(LocalFS(empty_sha256_root))
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


class TestStageCommitOtsCorruptedCache:
    def test_commit_refuses_when_plan_digest_does_not_match_verified_digest(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)

        staging = tmp_path / "staging"
        sd, _da, inv_digest, _ots_bytes = setup_at_inventory_upgraded(
            runner,
            empty_sha256_root,
            staging,
            "obj:commit-plan-mismatch",
            sp_fake,
            btc_fake.rpc,
        )
        other_sd, _other_da, other_inv_digest, _other_ots_bytes = (
            setup_at_inventory_upgraded(
                runner,
                empty_sha256_root,
                staging,
                "obj:commit-plan-other",
                sp_fake,
                btc_fake.rpc,
            )
        )
        assert other_inv_digest != inv_digest
        plan_path(sd).write_bytes(plan_path(other_sd).read_bytes())

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "commit",
                "obj:commit-plan-mismatch",
                "--allow-missing-metadata",
            ),
        )

        assert r.exit_code == ExitCode.PHASE, r.output
        out = (r.output or "").lower()
        assert "does not match verified inventory digest" in out, r.output
        assert "ocfl stage timestamp obj:commit-plan-mismatch" in out, r.output
        assert "ocfl stage upgrade obj:commit-plan-mismatch" in out, r.output

        store = StagingStore(staging)
        root_path = str(empty_sha256_root.resolve())
        assert store.exists(root_path, "obj:commit-plan-mismatch")
        sr = StorageRoot.open(LocalFS(empty_sha256_root))
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
        write_config(tmp_path, monkeypatch)

        staging = tmp_path / "staging"
        sd, _da, inv_digest, _ots_bytes = setup_at_inventory_upgraded(
            runner,
            empty_sha256_root,
            staging,
            "obj:commit-corrupt",
            sp_fake,
            btc_fake.rpc,
        )

        bad_bytes = build_attestation(
            bytes.fromhex(_OTHER_64HEX), block_height=DIGEST_BLOCK_HEIGHT
        )
        ots_cache_path(sd, inv_digest).write_bytes(bad_bytes)

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "commit",
                "obj:commit-corrupt",
            ),
        )
        assert r.exit_code == ExitCode.ATTESTATION, r.output
        out = (r.output or "").lower()
        assert "ocfl stage upgrade" in out, r.output

        store = StagingStore(staging)
        root_path = str(empty_sha256_root.resolve())
        assert store.exists(root_path, "obj:commit-corrupt")
        assert load_ots(sd).phase is Phase.INVENTORY_UPGRADED
        assert plan_path(sd).is_file()


class TestStageCommitOtsMissingCache:
    def test_commit_refuses_with_re_run_hint_when_cache_absent(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)

        staging = tmp_path / "staging"
        sd, _da, inv_digest, _ots_bytes = setup_at_inventory_upgraded(
            runner,
            empty_sha256_root,
            staging,
            "obj:commit-missing",
            sp_fake,
            btc_fake.rpc,
        )

        ots_cache_path(sd, inv_digest).unlink()

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "commit",
                "obj:commit-missing",
            ),
        )
        assert r.exit_code == ExitCode.PHASE, r.output
        out = (r.output or "").lower()
        assert "ocfl stage upgrade" in out, r.output

        store = StagingStore(staging)
        root_path = str(empty_sha256_root.resolve())
        assert store.exists(root_path, "obj:commit-missing")
        assert load_ots(sd).phase is Phase.INVENTORY_UPGRADED


class TestStageCommitOtsPreExecuteErrors:
    def test_changed_staged_file_reports_message_and_preserves_staging(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)

        staging = tmp_path / "staging"
        _sd, _da, _inv_digest, _ots_bytes = setup_at_inventory_upgraded(
            runner,
            empty_sha256_root,
            staging,
            "obj:commit-changed-source",
            sp_fake,
            btc_fake.rpc,
        )
        store = StagingStore(staging)
        root_path = str(empty_sha256_root.resolve())
        content_map = store.load_content_map(root_path, "obj:commit-changed-source")
        staged_src = Path(next(iter(content_map.values()))["path"])
        staged_src.write_text("changed content")

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "commit",
                "obj:commit-changed-source",
            ),
        )

        assert r.exit_code == ExitCode.GENERIC, r.output
        assert isinstance(r.exception, SystemExit)
        assert "changed" in r.output.lower()
        assert store.exists(root_path, "obj:commit-changed-source")
        assert (
            not StorageRoot.open(LocalFS(empty_sha256_root))
            .get_object("obj:commit-changed-source")
            .exists
        )

    def test_missing_staged_file_reports_message_and_preserves_staging(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)

        staging = tmp_path / "staging"
        _sd, _da, _inv_digest, _ots_bytes = setup_at_inventory_upgraded(
            runner,
            empty_sha256_root,
            staging,
            "obj:commit-missing-source",
            sp_fake,
            btc_fake.rpc,
        )
        store = StagingStore(staging)
        root_path = str(empty_sha256_root.resolve())
        content_map = store.load_content_map(root_path, "obj:commit-missing-source")
        staged_src = Path(next(iter(content_map.values()))["path"])
        staged_src.unlink()

        r = runner.invoke(
            app,
            stage_args(
                empty_sha256_root,
                staging,
                "commit",
                "obj:commit-missing-source",
            ),
        )

        assert r.exit_code == ExitCode.GENERIC, r.output
        assert isinstance(r.exception, SystemExit)
        assert "missing" in r.output.lower()
        assert store.exists(root_path, "obj:commit-missing-source")
        assert (
            not StorageRoot.open(LocalFS(empty_sha256_root))
            .get_object("obj:commit-missing-source")
            .exists
        )

    def test_lock_timeout_reports_canonical_exit_and_preserves_staging(
        self,
        runner: CliRunner,
        empty_sha256_root: Path,
        sp_fake: FakeSimpleProof,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)

        staging = tmp_path / "staging"
        setup_at_inventory_upgraded(
            runner,
            empty_sha256_root,
            staging,
            "obj:commit-lock-timeout",
            sp_fake,
            btc_fake.rpc,
        )
        root_path = str(empty_sha256_root.resolve())
        runner_obj(runner)["lock_timeout"] = 0.01

        with locks.object_commit_lock(
            root_path, "obj:commit-lock-timeout", lock_runtime(30)
        ):
            r = runner.invoke(
                app,
                stage_args(
                    empty_sha256_root,
                    staging,
                    "commit",
                    "obj:commit-lock-timeout",
                ),
            )

        assert r.exit_code == ExitCode.LOCK_TIMEOUT, r.output
        assert isinstance(r.exception, SystemExit)
        assert "lock" in r.output.lower()
        store = StagingStore(staging)
        assert store.exists(root_path, "obj:commit-lock-timeout")
        assert (
            not StorageRoot.open(LocalFS(empty_sha256_root))
            .get_object("obj:commit-lock-timeout")
            .exists
        )


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
        write_config(tmp_path, monkeypatch)

        staging = tmp_path / "staging"
        _sd, _da, _inv_digest, _ots_bytes = setup_at_inventory_upgraded(
            runner,
            empty_sha256_root,
            staging,
            "obj:commit-sigint",
            sp_fake,
            btc_fake.rpc,
        )

        with injected(runner, fs_factory=raise_on_content_write(KeyboardInterrupt())):
            r = runner.invoke(
                app,
                stage_args(
                    empty_sha256_root,
                    staging,
                    "commit",
                    "obj:commit-sigint",
                ),
            )

        assert r.exit_code == ExitCode.INTERRUPTED, r.output
        assert "interrupted" in (r.output or "").lower(), r.output

        store = StagingStore(staging)
        root_path = str(empty_sha256_root.resolve())
        assert store.exists(root_path, "obj:commit-sigint")

        obj_dir = empty_sha256_root / "obj:commit-sigint"
        ext_file = (
            obj_dir / "extensions" / "NNNN-ots-timestamps" / "inventory.json.v1.ots"
        )
        assert not ext_file.exists(), f"extension file leaked at {ext_file}"
        assert not (obj_dir / "inventory.json").exists()


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
        write_config(tmp_path, monkeypatch)

        staging = tmp_path / "staging"
        sd, _da, inv_digest, ots_bytes = setup_at_inventory_upgraded(
            runner,
            empty_sha256_root,
            staging,
            "obj:commit-crash",
            sp_fake,
            btc_fake.rpc,
        )

        with injected(
            runner,
            fs_factory=raise_on_content_write(RuntimeError("simulated crash")),
        ):
            r1 = runner.invoke(
                app,
                stage_args(
                    empty_sha256_root,
                    staging,
                    "commit",
                    "obj:commit-crash",
                ),
            )
        assert r1.exit_code == ExitCode.GENERIC, r1.output

        store = StagingStore(staging)
        root_path = str(empty_sha256_root.resolve())
        assert store.exists(root_path, "obj:commit-crash")
        assert plan_path(sd).is_file()
        assert ots_cache_path(sd, inv_digest).read_bytes() == ots_bytes
        assert load_ots(sd).phase is Phase.INVENTORY_UPGRADED

        r2 = runner.invoke(
            app,
            stage_args(empty_sha256_root, staging, "commit", "obj:commit-crash"),
        )
        assert r2.exit_code == ExitCode.SUCCESS, r2.output

        sr = StorageRoot.open(LocalFS(empty_sha256_root))
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
