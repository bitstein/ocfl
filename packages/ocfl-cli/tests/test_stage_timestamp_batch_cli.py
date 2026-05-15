"""CLI tests for local-only batch timestamping commands."""

import json
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from ocfl_cli.main import app
from ocfl_cli.stage.batch import (
    BatchFile,
    BatchObject,
    BatchObjectStatus,
    BatchSubmission,
    BatchVerifyJob,
    batch_file_path,
    new_batch_id,
    save_batch,
)
from ocfl_cli.stage.fsm import Phase
from ocfl_cli.stage.state import (
    OtsState,
    Submission,
    VerifiedProof,
    VerifyJob,
    save_ots,
)
from ocfl_cli.stage.store import StagingStore


def _invoke(runner: CliRunner, storage_root: Path, staging: Path, *args: str):
    return runner.invoke(
        app,
        ["--root", str(storage_root), "stage", "--staging-root", str(staging), *args],
    )


def _invoke_json(runner: CliRunner, storage_root: Path, staging: Path, *args: str):
    return runner.invoke(
        app,
        [
            "--root",
            str(storage_root),
            "--json",
            "stage",
            "--staging-root",
            str(staging),
            *args,
        ],
    )


def _ots_staging(
    staging_root: Path,
    root_path: str,
    object_id: str,
    state: OtsState,
) -> Path:
    store = StagingStore(staging_root)
    staging_dir = store.staging_path(root_path, object_id)
    staging_dir.mkdir(parents=True)
    save_ots(staging_dir, state)
    return staging_dir


def _status_json_and_human(
    runner: CliRunner, storage_root: Path, staging: Path, batch_id: str
) -> tuple[dict, str]:
    json_result = _invoke_json(
        runner, storage_root, staging, "timestamp", "batch", "status", batch_id
    )
    human_result = _invoke(
        runner, storage_root, staging, "timestamp", "batch", "status", batch_id
    )
    assert json_result.exit_code == 0, json_result.output
    assert human_result.exit_code == 0, human_result.output
    return json.loads(json_result.output), human_result.output


def _assert_next_consistent(
    data: dict, output: str, *, object_id: str, expected: str
) -> None:
    rows = {row["object_id"]: row for row in data["objects"]}
    assert rows[object_id]["next"] == expected
    assert f"{object_id}  " in output
    assert f"Next: {expected}" in output


def test_batch_ls_json_orders_by_updated_at(
    runner: CliRunner, storage_root: Path, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    first_id = new_batch_id()
    second_id = new_batch_id()
    save_batch(
        staging,
        BatchFile(
            batch_id=first_id,
            target="content",
            root_path=str(storage_root),
            category_id=641,
            created_at="2026-05-12T10:00:00Z",
            updated_at="2026-05-12T10:00:00Z",
            objects=(BatchObject(object_id="obj:1", status="upgraded"),),
        ),
    )
    save_batch(
        staging,
        BatchFile(
            batch_id=second_id,
            target="inventory",
            root_path=str(storage_root),
            category_id=642,
            created_at="2026-05-12T10:01:00Z",
            updated_at="2026-05-12T10:05:00Z",
            objects=(
                BatchObject(object_id="obj:2", status="prepared"),
                BatchObject(object_id="obj:3", status="skipped", skipped_reason="noop"),
            ),
        ),
    )

    result = _invoke_json(runner, storage_root, staging, "timestamp", "batch", "ls")

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert [row["batch_id"] for row in data] == [second_id, first_id]
    assert data[0]["counts"] == {
        "prepared": 1,
        "submitted": 0,
        "upgraded": 0,
        "failed": 0,
        "skipped": 1,
        "total": 2,
    }
    assert data[0]["target"] == "inventory"
    assert data[0]["category_id"] == 642


def test_batch_ls_human_includes_created_at(
    runner: CliRunner, storage_root: Path, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    batch_id = new_batch_id()
    save_batch(
        staging,
        BatchFile(
            batch_id=batch_id,
            target="content",
            root_path=str(storage_root),
            category_id=641,
            created_at="2026-05-12T10:00:00Z",
            updated_at="2026-05-12T10:05:00Z",
            objects=(BatchObject(object_id="obj:1", status="upgraded"),),
        ),
    )

    result = _invoke(runner, storage_root, staging, "timestamp", "batch", "ls")

    assert result.exit_code == 0, result.output
    assert batch_id.split("-", 1)[0] in result.output
    assert "2026-05-12T10:00:00Z" in result.output


def test_batch_status_json_contract_uses_canonical_next(
    runner: CliRunner, storage_root: Path, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    batch_id = new_batch_id()
    root_path = str(storage_root)
    digest_a = "sha256:" + ("a" * 64)
    digest_b = "sha256:" + ("b" * 64)
    _ots_staging(
        staging,
        root_path,
        "obj:1",
        OtsState(
            phase=Phase.CONTENT_PENDING,
            category_id=641,
            submissions=(
                Submission(
                    submitted_at="2026-05-12T10:01:00Z",
                    digests=(digest_a, digest_b),
                    status="submitted",
                    batch_id=batch_id,
                    record_ids=("rec-a", "rec-b"),
                ),
            ),
            verify_jobs=(
                VerifyJob(
                    job_id="verify-1",
                    digests=(digest_a, digest_b),
                    status="IN_PROGRESS",
                ),
            ),
            content_proofs={digest_a: VerifiedProof(verified=True)},
        ),
    )
    save_batch(
        staging,
        BatchFile(
            batch_id=batch_id,
            target="content",
            root_path=root_path,
            category_id=641,
            created_at="2026-05-12T10:00:00Z",
            updated_at="2026-05-12T10:05:00Z",
            objects=(
                BatchObject(
                    object_id="obj:1",
                    status="submitted",
                    digests=(digest_a, digest_b),
                ),
            ),
            submissions=(
                BatchSubmission(
                    chunk_index=0,
                    digests=(digest_a, digest_b),
                    status="submitted",
                    record_ids=("rec-a", "rec-b"),
                    submitted_at="2026-05-12T10:01:00Z",
                    attempts=1,
                ),
            ),
            verify_jobs=(
                BatchVerifyJob(
                    job_id="verify-1",
                    digests=(digest_a, digest_b),
                    status="IN_PROGRESS",
                    submitted_at="2026-05-12T10:03:00Z",
                    attempts=1,
                ),
            ),
        ),
    )

    result = _invoke_json(
        runner, storage_root, staging, "timestamp", "batch", "status", batch_id
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["batch_id"] == batch_id
    assert data["counts"]["submitted"] == 1
    assert data["submissions"][0]["record_ids"] == ["rec-a", "rec-b"]
    assert data["verify_jobs"][0]["results_url_expires_at"] is None
    assert data["objects"] == [
        {
            "object_id": "obj:1",
            "phase": "CONTENT_PENDING",
            "batch_status": "submitted",
            "digests_total": 2,
            "digests_verified": 1,
            "digests_submitted": 2,
            "last_error": None,
            "skipped_reason": None,
            "next": "Re-run --upgrade later",
            "batch_id": batch_id,
        }
    ]


def test_batch_status_healthy_next_strings_are_canonical(
    runner: CliRunner, storage_root: Path, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    batch_id = new_batch_id()
    root_path = str(storage_root)
    digest_a = "sha256:" + ("a" * 64)
    digest_b = "sha256:" + ("b" * 64)
    digest_c = "sha256:" + ("c" * 64)
    digest_d = "sha256:" + ("d" * 64)
    digest_e = "sha256:" + ("e" * 64)
    digest_f = "sha256:" + ("f" * 64)
    save_batch(
        staging,
        BatchFile(
            batch_id=batch_id,
            target="content",
            root_path=root_path,
            category_id=641,
            created_at="2026-05-12T10:00:00Z",
            updated_at="2026-05-12T10:05:00Z",
            objects=(
                BatchObject(object_id="obj:prepared", status="prepared"),
                BatchObject(
                    object_id="obj:partial",
                    status="submitted",
                    digests=(digest_a, digest_b),
                ),
                BatchObject(
                    object_id="obj:needs-upgrade",
                    status="submitted",
                    digests=(digest_c,),
                ),
                BatchObject(
                    object_id="obj:in-progress",
                    status="submitted",
                    digests=(digest_d,),
                ),
                BatchObject(
                    object_id="obj:complete",
                    status="upgraded",
                    digests=(digest_e,),
                ),
            ),
            submissions=(
                BatchSubmission(
                    chunk_index=0,
                    digests=(digest_a, digest_b),
                    status="submitted",
                    record_ids=("rec-a",),
                ),
                BatchSubmission(
                    chunk_index=1,
                    digests=(digest_c,),
                    status="submitted",
                    record_ids=("rec-c",),
                ),
                BatchSubmission(
                    chunk_index=2,
                    digests=(digest_d,),
                    status="submitted",
                    record_ids=("rec-d",),
                ),
                BatchSubmission(
                    chunk_index=3,
                    digests=(digest_e,),
                    status="submitted",
                    record_ids=("rec-e",),
                ),
            ),
            verify_jobs=(
                BatchVerifyJob(
                    job_id="verify-d",
                    digests=(digest_d,),
                    status="IN_PROGRESS",
                    submitted_at="2026-05-12T10:03:00Z",
                ),
            ),
        ),
    )
    complete_batch_id = new_batch_id()
    save_batch(
        staging,
        BatchFile(
            batch_id=complete_batch_id,
            target="content",
            root_path=root_path,
            category_id=641,
            created_at="2026-05-12T10:00:00Z",
            updated_at="2026-05-12T10:05:00Z",
            objects=(
                BatchObject(
                    object_id="obj:batch-complete",
                    status="upgraded",
                    digests=(digest_f,),
                ),
            ),
        ),
    )

    data, output = _status_json_and_human(runner, storage_root, staging, batch_id)

    _assert_next_consistent(
        data,
        output,
        object_id="obj:prepared",
        expected=f"Run --batch {batch_id} to submit",
    )
    _assert_next_consistent(
        data,
        output,
        object_id="obj:partial",
        expected=f"Run --batch {batch_id} --retry",
    )
    _assert_next_consistent(
        data,
        output,
        object_id="obj:needs-upgrade",
        expected=f"Run --batch {batch_id} --upgrade",
    )
    _assert_next_consistent(
        data,
        output,
        object_id="obj:in-progress",
        expected="Re-run --upgrade later",
    )
    _assert_next_consistent(
        data,
        output,
        object_id="obj:complete",
        expected="Object complete",
    )

    complete_data, complete_output = _status_json_and_human(
        runner, storage_root, staging, complete_batch_id
    )
    _assert_next_consistent(
        complete_data,
        complete_output,
        object_id="obj:batch-complete",
        expected=f"Batch complete; batch rm {complete_batch_id} to clean up",
    )


@pytest.mark.parametrize(
    ("reason", "status", "expected_template"),
    [
        ("regressed", "failed", "stage status OBJECT; reset or restart timestamp flow"),
        (
            "missing_record_ids",
            "failed",
            "stage timestamp batch content --batch {batch_id} --retry",
        ),
        (
            "lock_contended",
            "skipped",
            "Re-run --batch {batch_id} after the other process exits",
        ),
        (
            "in_batch=018f1f7e-7b4a-7c6a-8a2b-4a7f4c2e9d10",
            "skipped",
            "stage timestamp batch status 018f1f7e-7b4a-7c6a-8a2b-4a7f4c2e9d10",
        ),
        (
            "category_mismatch=642",
            "skipped",
            "Start a separate batch for that category or reset/recreate the "
            "staging session",
        ),
        ("committed", "skipped", "None"),
        ("no_new_digests", "skipped", "None"),
        ("noop", "skipped", "None"),
        ("content_not_upgraded", "skipped", "Upgrade content first"),
        (
            "missing_metadata",
            "skipped",
            "Add staged metadata or rerun with --allow-missing-metadata",
        ),
        ("sp_partial_results", "submitted", "Re-run --upgrade later"),
        ("results_expired", "submitted", "Re-run --upgrade"),
        ("verify_job_failed", "submitted", "Re-run --upgrade"),
        (
            "transient_sp_error",
            "prepared",
            "stage timestamp batch content --batch {batch_id} --retry",
        ),
        ("transient_sp_error", "submitted", "Re-run --upgrade"),
        ("transient_rpc_error", "submitted", "Re-run --upgrade"),
        (
            "reorg_deeper_than_min_conf",
            "failed",
            "Retry after node catches up or investigate chain state",
        ),
        ("attestation_mismatch", "failed", "Operator inspection required"),
        (
            "batch_file_missing",
            "failed",
            "Operate per-object via stage timestamp content/inventory",
        ),
        (
            "submit_pending",
            "submitted",
            "stage timestamp batch content --batch {batch_id} --retry",
        ),
    ],
)
def test_batch_status_reason_next_strings_are_consistent(
    runner: CliRunner,
    storage_root: Path,
    tmp_path: Path,
    reason: str,
    status: str,
    expected_template: str,
) -> None:
    staging = tmp_path / "staging"
    batch_id = new_batch_id()
    object_id = "obj:reason"
    status_value = cast(BatchObjectStatus, status)
    row_kwargs = {
        "object_id": object_id,
        "status": status_value,
        "digests": (),
        "last_error": None,
        "skipped_reason": None,
    }
    if status == "skipped":
        row_kwargs["skipped_reason"] = reason
    else:
        row_kwargs["last_error"] = reason
    save_batch(
        staging,
        BatchFile(
            batch_id=batch_id,
            target="content",
            root_path=str(storage_root),
            category_id=641,
            created_at="2026-05-12T10:00:00Z",
            updated_at="2026-05-12T10:05:00Z",
            objects=(BatchObject(**row_kwargs),),
        ),
    )

    data, output = _status_json_and_human(runner, storage_root, staging, batch_id)

    _assert_next_consistent(
        data,
        output,
        object_id=object_id,
        expected=expected_template.format(batch_id=batch_id),
    )


def test_batch_status_verbose_json_adds_per_digest_state(
    runner: CliRunner, storage_root: Path, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    batch_id = new_batch_id()
    root_path = str(storage_root)
    digest_a = "sha256:" + ("a" * 64)
    digest_b = "sha256:" + ("b" * 64)
    _ots_staging(
        staging,
        root_path,
        "obj:1",
        OtsState(
            phase=Phase.CONTENT_PENDING,
            category_id=641,
            submissions=(
                Submission(
                    submitted_at="2026-05-12T10:01:00Z",
                    digests=(digest_a, digest_b),
                    status="submitting",
                    batch_id=batch_id,
                    record_ids=("rec-a", None),
                ),
            ),
            content_proofs={digest_a: VerifiedProof(verified=True)},
        ),
    )
    save_batch(
        staging,
        BatchFile(
            batch_id=batch_id,
            target="content",
            root_path=root_path,
            category_id=641,
            created_at="2026-05-12T10:00:00Z",
            updated_at="2026-05-12T10:05:00Z",
            objects=(
                BatchObject(
                    object_id="obj:1",
                    status="submitted",
                    digests=(digest_a, digest_b),
                ),
            ),
            submissions=(
                BatchSubmission(
                    chunk_index=0,
                    digests=(digest_a, digest_b),
                    status="pending",
                    record_ids=None,
                ),
            ),
        ),
    )

    result = _invoke_json(
        runner,
        storage_root,
        staging,
        "timestamp",
        "batch",
        "status",
        batch_id,
        "--verbose",
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["objects"][0]["digests"] == [
        {
            "digest": digest_a,
            "submitted": True,
            "verified": True,
            "record_id": "rec-a",
            "submit_status": "pending",
            "verify_status": None,
        },
        {
            "digest": digest_b,
            "submitted": False,
            "verified": False,
            "record_id": None,
            "submit_status": "pending",
            "verify_status": None,
        },
    ]


def test_batch_status_missing_file_exits_2(
    runner: CliRunner, storage_root: Path, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    batch_id = new_batch_id()

    result = _invoke_json(
        runner, storage_root, staging, "timestamp", "batch", "status", batch_id
    )

    assert result.exit_code == 2
    data = json.loads(result.output)
    assert data["reason"] == "batch_file_missing"
    assert "stage timestamp content/inventory" in data["next"]


def test_batch_status_corrupt_existing_file_is_not_missing(
    runner: CliRunner, storage_root: Path, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    batch_id = new_batch_id()
    path = staging / "batches" / f"{batch_id}.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not-json")

    result = _invoke(
        runner, storage_root, staging, "timestamp", "batch", "status", batch_id
    )

    assert result.exit_code == 1
    assert "batch_file_missing" not in result.output
    assert "invalid" in result.output or "JSON" in result.output


def test_batch_status_next_distinguishes_transient_sp_context(
    runner: CliRunner, storage_root: Path, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    batch_id = new_batch_id()
    save_batch(
        staging,
        BatchFile(
            batch_id=batch_id,
            target="content",
            root_path=str(storage_root),
            category_id=641,
            created_at="2026-05-12T10:00:00Z",
            updated_at="2026-05-12T10:05:00Z",
            objects=(
                BatchObject(
                    object_id="obj:1",
                    status="prepared",
                    last_error="transient_sp_error",
                ),
                BatchObject(
                    object_id="obj:2",
                    status="submitted",
                    last_error="transient_sp_error",
                ),
            ),
        ),
    )

    result = _invoke_json(
        runner, storage_root, staging, "timestamp", "batch", "status", batch_id
    )

    assert result.exit_code == 0, result.output
    rows = {row["object_id"]: row for row in json.loads(result.output)["objects"]}
    assert rows["obj:1"]["next"] == (
        f"stage timestamp batch content --batch {batch_id} --retry"
    )
    assert rows["obj:2"]["next"] == "Re-run --upgrade"


def test_batch_rm_refuses_non_terminal_without_force(
    runner: CliRunner, storage_root: Path, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    batch_id = new_batch_id()
    save_batch(
        staging,
        BatchFile(
            batch_id=batch_id,
            target="content",
            root_path=str(storage_root),
            category_id=641,
            created_at="2026-05-12T10:00:00Z",
            updated_at="2026-05-12T10:05:00Z",
            objects=(BatchObject(object_id="obj:1", status="prepared"),),
        ),
    )

    result = _invoke(
        runner, storage_root, staging, "timestamp", "batch", "rm", batch_id
    )

    assert result.exit_code == 1
    assert batch_file_path(staging, batch_id).is_file()

    forced = _invoke(
        runner, storage_root, staging, "timestamp", "batch", "rm", batch_id, "--force"
    )

    assert forced.exit_code == 0, forced.output
    assert not batch_file_path(staging, batch_id).exists()
