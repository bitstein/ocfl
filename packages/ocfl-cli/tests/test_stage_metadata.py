from pathlib import Path
from typing import Any

import pytest
from fakes.locks import is_flocked, lock_runtime
from msgspec.structs import replace
from runner_helpers import ObjRunner
from typer.testing import Result

from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app
from ocfl_cli.model.metadata import StagedMetadata, StagedUser
from ocfl_cli.model.ots_state import Proof, StagedInventory, VerifyJob
from ocfl_cli.model.phase import Phase, TimestampTarget
from ocfl_cli.store.ots_state import load_ots, plan_path, save_ots, with_lock
from ocfl_cli.store.staging import StagingStore


def _invoke(runner: ObjRunner, storage_root: Path, staging: Path, *args: str) -> Result:
    return runner.invoke(
        app,
        ["--root", str(storage_root), "stage", "--staging-root", str(staging), *args],
    )


def _metadata(storage_root: Path, staging: Path, object_id: str) -> StagedMetadata:
    store = StagingStore(staging)
    return store.load_metadata(str(storage_root.resolve()), object_id)


def _write_ots_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "ocfl-config" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        """\
[simpleproof]
base_url = "https://stage.example"
proof_category_id = 641

[bitcoin]
rpc_url = "http://127.0.0.1:8332"
zmq_endpoint = "tcp://127.0.0.1:28332"
"""
    )
    monkeypatch.setenv("OCFL_CONFIG", str(cfg))


class TestStageNewMetadata:
    def test_new_accepts_initial_metadata(
        self, runner: ObjRunner, storage_root: Path, tmp_path: Path
    ) -> None:
        staging = tmp_path / "staging"

        result = _invoke(
            runner,
            storage_root,
            staging,
            "new",
            "obj:new",
            "-m",
            "Initial deposit",
            "--name",
            "Alice",
            "--address",
            "mailto:alice@example.org",
        )

        assert result.exit_code == ExitCode.SUCCESS, result.output
        assert _metadata(storage_root, staging, "obj:new") == StagedMetadata(
            message="Initial deposit",
            user=StagedUser(name="Alice", address="mailto:alice@example.org"),
        )

    def test_new_ots_accepts_initial_metadata(
        self,
        runner: ObjRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_ots_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"

        result = _invoke(
            runner,
            storage_root,
            staging,
            "new",
            "obj:ots",
            "--ots",
            "-m",
            "Timestamped deposit",
            "--name",
            "Alice",
            "--address",
            "mailto:alice@example.org",
        )

        assert result.exit_code == ExitCode.SUCCESS, result.output
        assert _metadata(storage_root, staging, "obj:ots") == StagedMetadata(
            message="Timestamped deposit",
            user=StagedUser(name="Alice", address="mailto:alice@example.org"),
        )

    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            (("--message", " "), "message"),
            (("--name", " "), "name"),
            (("--address", " "), "address"),
        ],
    )
    def test_new_rejects_empty_metadata_value_without_creating_staging(
        self,
        runner: ObjRunner,
        storage_root: Path,
        tmp_path: Path,
        args: tuple[str, ...],
        expected: str,
    ) -> None:
        staging = tmp_path / "staging"

        result = _invoke(
            runner,
            storage_root,
            staging,
            "new",
            "obj:new",
            *args,
        )

        assert result.exit_code == ExitCode.USAGE
        assert expected in result.output.lower()
        store = StagingStore(staging)
        assert not store.exists(str(storage_root.resolve()), "obj:new")


class TestStageMetadata:
    def test_metadata_updates_only_supplied_fields(
        self, runner: ObjRunner, storage_root: Path, tmp_path: Path
    ) -> None:
        staging = tmp_path / "staging"
        assert (
            _invoke(
                runner,
                storage_root,
                staging,
                "new",
                "obj:new",
                "-m",
                "Initial",
                "--name",
                "Alice",
                "--address",
                "mailto:alice@example.org",
            ).exit_code
            == ExitCode.SUCCESS
        )

        result = _invoke(
            runner,
            storage_root,
            staging,
            "metadata",
            "obj:new",
            "--name",
            "Bob",
        )

        assert result.exit_code == ExitCode.SUCCESS, result.output
        assert _metadata(storage_root, staging, "obj:new") == StagedMetadata(
            message="Initial",
            user=StagedUser(name="Bob", address="mailto:alice@example.org"),
        )

    def test_metadata_clears_fields(
        self, runner: ObjRunner, storage_root: Path, tmp_path: Path
    ) -> None:
        staging = tmp_path / "staging"
        assert (
            _invoke(
                runner,
                storage_root,
                staging,
                "new",
                "obj:new",
                "-m",
                "Initial",
                "--name",
                "Alice",
                "--address",
                "mailto:alice@example.org",
            ).exit_code
            == ExitCode.SUCCESS
        )

        result = _invoke(
            runner,
            storage_root,
            staging,
            "metadata",
            "obj:new",
            "--clear-message",
            "--clear-address",
        )

        assert result.exit_code == ExitCode.SUCCESS, result.output
        assert _metadata(storage_root, staging, "obj:new") == StagedMetadata(
            user=StagedUser(name="Alice")
        )

    def test_clear_name_also_clears_address_unless_address_is_set(
        self, runner: ObjRunner, storage_root: Path, tmp_path: Path
    ) -> None:
        staging = tmp_path / "staging"
        assert (
            _invoke(
                runner,
                storage_root,
                staging,
                "new",
                "obj:new",
                "--name",
                "Alice",
                "--address",
                "mailto:alice@example.org",
            ).exit_code
            == ExitCode.SUCCESS
        )

        result = _invoke(
            runner,
            storage_root,
            staging,
            "metadata",
            "obj:new",
            "--clear-name",
            "--address",
            "mailto:bob@example.org",
        )

        assert result.exit_code == ExitCode.SUCCESS, result.output
        assert _metadata(storage_root, staging, "obj:new") == StagedMetadata(
            user=StagedUser(address="mailto:bob@example.org")
        )

        result = _invoke(
            runner,
            storage_root,
            staging,
            "metadata",
            "obj:new",
            "--clear-name",
        )

        assert result.exit_code == ExitCode.SUCCESS, result.output
        assert _metadata(storage_root, staging, "obj:new") == StagedMetadata()

    @pytest.mark.parametrize(
        "args",
        [
            ("--message", "updated", "--clear-message"),
            ("--name", "Bob", "--clear-name"),
            ("--address", "mailto:bob@example.org", "--clear-address"),
        ],
    )
    def test_metadata_rejects_set_and_clear_conflict(
        self,
        runner: ObjRunner,
        storage_root: Path,
        tmp_path: Path,
        args: tuple[str, ...],
    ) -> None:
        staging = tmp_path / "staging"
        assert (
            _invoke(runner, storage_root, staging, "new", "obj:new").exit_code
            == ExitCode.SUCCESS
        )

        result = _invoke(runner, storage_root, staging, "metadata", "obj:new", *args)

        assert result.exit_code == ExitCode.USAGE
        assert "incompatible" in result.output.lower()

    def test_metadata_rejects_empty_value(
        self, runner: ObjRunner, storage_root: Path, tmp_path: Path
    ) -> None:
        staging = tmp_path / "staging"
        assert (
            _invoke(runner, storage_root, staging, "new", "obj:new").exit_code
            == ExitCode.SUCCESS
        )

        result = _invoke(
            runner,
            storage_root,
            staging,
            "metadata",
            "obj:new",
            "--name",
            "\t",
        )

        assert result.exit_code == ExitCode.USAGE
        assert "name" in result.output.lower()

    def test_metadata_updates_staging_without_metadata_file(
        self, runner: ObjRunner, storage_root: Path, tmp_path: Path
    ) -> None:
        staging = tmp_path / "staging"
        assert (
            _invoke(runner, storage_root, staging, "new", "obj:new").exit_code
            == ExitCode.SUCCESS
        )
        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        (store.staging_path(root_path, "obj:new") / "metadata.json").unlink()

        result = _invoke(
            runner,
            storage_root,
            staging,
            "metadata",
            "obj:new",
            "--message",
            "Recovered metadata",
        )

        assert result.exit_code == ExitCode.SUCCESS, result.output
        assert _metadata(storage_root, staging, "obj:new") == StagedMetadata(
            message="Recovered metadata"
        )

    def test_metadata_command_holds_staging_lock_during_update(
        self,
        runner: ObjRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        staging = tmp_path / "staging"
        assert (
            _invoke(runner, storage_root, staging, "new", "obj:new").exit_code
            == ExitCode.SUCCESS
        )
        store = StagingStore(staging)
        staging_dir = store.staging_path(str(storage_root.resolve()), "obj:new")
        original_save_metadata = StagingStore.save_metadata
        observed_lock = False

        def assert_locked(self: StagingStore, *args: Any, **kwargs: Any) -> None:
            nonlocal observed_lock
            assert is_flocked(staging_dir / ".lock")
            observed_lock = True
            original_save_metadata(self, *args, **kwargs)

        monkeypatch.setattr(StagingStore, "save_metadata", assert_locked)

        result = _invoke(
            runner,
            storage_root,
            staging,
            "metadata",
            "obj:new",
            "--message",
            "Locked update",
        )

        assert result.exit_code == ExitCode.SUCCESS, result.output
        assert observed_lock

    def test_metadata_command_reports_lock_timeout(
        self,
        runner: ObjRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        staging = tmp_path / "staging"
        assert (
            _invoke(runner, storage_root, staging, "new", "obj:new").exit_code
            == ExitCode.SUCCESS
        )
        store = StagingStore(staging)
        staging_dir = store.staging_path(str(storage_root.resolve()), "obj:new")
        runner.obj["lock_timeout"] = 0.01

        with with_lock(staging_dir, lock_runtime(1)):
            result = _invoke(
                runner,
                storage_root,
                staging,
                "metadata",
                "obj:new",
                "--message",
                "Blocked update",
            )

        assert result.exit_code == ExitCode.LOCK_TIMEOUT
        assert "lock" in result.output.lower()

    def test_metadata_usage_error_precedes_lock_timeout(
        self,
        runner: ObjRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        staging = tmp_path / "staging"
        assert (
            _invoke(runner, storage_root, staging, "new", "obj:new").exit_code
            == ExitCode.SUCCESS
        )
        store = StagingStore(staging)
        staging_dir = store.staging_path(str(storage_root.resolve()), "obj:new")
        runner.obj["lock_timeout"] = 0.01

        with with_lock(staging_dir, lock_runtime(1)):
            result = _invoke(
                runner,
                storage_root,
                staging,
                "metadata",
                "obj:new",
                "--name",
                "Bob",
                "--clear-name",
            )

        assert result.exit_code == ExitCode.USAGE
        assert "incompatible" in result.output.lower()

    @pytest.mark.parametrize(
        "phase", [Phase.INVENTORY_PENDING, Phase.INVENTORY_UPGRADED]
    )
    def test_metadata_edit_invalidates_inventory_timestamp_state(
        self,
        runner: ObjRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        phase: Phase,
    ) -> None:
        _write_ots_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        assert (
            _invoke(
                runner,
                storage_root,
                staging,
                "new",
                "obj:ots",
                "--ots",
                "-m",
                "Initial",
                "--name",
                "Alice",
                "--address",
                "mailto:alice@example.org",
            ).exit_code
            == ExitCode.SUCCESS
        )
        store = StagingStore(staging)
        staging_dir = store.staging_path(str(storage_root.resolve()), "obj:ots")
        content_digest = "a" * 64
        inventory_digest = "b" * 64
        inventory_job = VerifyJob(
            job_id="inventory-job",
            digests=(inventory_digest,),
            status="IN_PROGRESS",
        )
        content_job = VerifyJob(
            job_id="content-job",
            digests=(content_digest,),
            status="IN_PROGRESS",
        )
        content_proof = Proof(
            target=TimestampTarget.CONTENT,
            ots_path=f"ots-cache/{content_digest}.ots",
            verified=True,
            block_height=800_000,
            confirmations_at_verify=3,
            verified_at="2026-04-23T16:30:00+00:00",
        )
        inv_proof = Proof(
            target=TimestampTarget.INVENTORY,
            verified=phase is Phase.INVENTORY_UPGRADED,
        )
        state = load_ots(staging_dir)
        save_ots(
            staging_dir,
            replace(
                state,
                phase=phase,
                pending_digests=(content_digest,),
                verify_jobs=(content_job, inventory_job),
                proofs={content_digest: content_proof, inventory_digest: inv_proof},
                inventory=StagedInventory(
                    version="v1",
                    digest=inventory_digest,
                    job_id="inventory-job",
                ),
            ),
        )
        plan_path(staging_dir).write_bytes(b'{"stale":true}')

        result = _invoke(
            runner,
            storage_root,
            staging,
            "metadata",
            "obj:ots",
            "--message",
            "Updated",
        )

        assert result.exit_code == ExitCode.SUCCESS, result.output
        updated_state = load_ots(staging_dir)
        assert updated_state.phase is Phase.CONTENT_UPGRADED
        assert updated_state.inventory is None
        assert updated_state.inventory_proof is None
        assert updated_state.verify_jobs == (content_job, inventory_job), (
            "ADR-0004: moving back keeps every verify job"
        )
        assert updated_state.proofs == {
            content_digest: content_proof,
            inventory_digest: inv_proof,
        }, "ADR-0004: moving back keeps every proof"
        assert updated_state.pending_digests == (content_digest,)
        assert plan_path(staging_dir).exists(), "ADR-0004: moving back keeps plan.json"

    def test_metadata_edit_invalidates_ots_state_before_metadata_write(
        self,
        runner: ObjRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_ots_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        assert (
            _invoke(
                runner,
                storage_root,
                staging,
                "new",
                "obj:ots",
                "--ots",
                "-m",
                "Initial",
                "--name",
                "Alice",
                "--address",
                "mailto:alice@example.org",
            ).exit_code
            == ExitCode.SUCCESS
        )
        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        staging_dir = store.staging_path(root_path, "obj:ots")
        inventory_digest = "b" * 64
        state = load_ots(staging_dir)
        save_ots(
            staging_dir,
            replace(
                state,
                phase=Phase.INVENTORY_UPGRADED,
                inventory=StagedInventory(
                    version="v1",
                    digest=inventory_digest,
                    job_id="inventory-job",
                ),
                proofs={
                    inventory_digest: Proof(
                        target=TimestampTarget.INVENTORY, verified=True
                    )
                },
                verify_jobs=(
                    VerifyJob(
                        job_id="inventory-job",
                        digests=(inventory_digest,),
                        status="IN_PROGRESS",
                    ),
                ),
            ),
        )
        plan_path(staging_dir).write_bytes(b'{"stale":true}')

        original_save_metadata = StagingStore.save_metadata
        observed_safe_order = False

        def fail_after_invalidation(
            self: StagingStore,
            save_root_path: str,
            save_object_id: str,
            metadata: StagedMetadata,
        ) -> None:
            nonlocal observed_safe_order
            if save_object_id == "obj:ots":
                observed_safe_order = True
                assert load_ots(staging_dir).phase is Phase.CONTENT_UPGRADED
                raise RuntimeError("injected metadata write failure")
            original_save_metadata(self, save_root_path, save_object_id, metadata)

        monkeypatch.setattr(StagingStore, "save_metadata", fail_after_invalidation)

        result = _invoke(
            runner,
            storage_root,
            staging,
            "metadata",
            "obj:ots",
            "--message",
            "Updated",
        )

        assert result.exit_code != ExitCode.SUCCESS
        assert observed_safe_order
        assert _metadata(storage_root, staging, "obj:ots").message == "Initial"
        updated_state = load_ots(staging_dir)
        assert updated_state.phase is Phase.CONTENT_UPGRADED
        assert updated_state.inventory is None
        assert [j.job_id for j in updated_state.verify_jobs] == ["inventory-job"]
        assert plan_path(staging_dir).exists()
