"""Tests for `ocfl stage commit --batch`."""

import json
import shutil
from pathlib import Path
from typing import get_args, get_type_hints, is_typeddict

import pytest
from msgspec.structs import replace
from typer.testing import CliRunner

from ocfl_cli.errors import AttestationMismatchError, PhaseError
from ocfl_cli.main import app
from ocfl_cli.model import batch as batch_model
from ocfl_cli.model.batch import BatchFile, BatchObject
from ocfl_cli.store import batch as batch_state
from ocfl_cli.store.batch import save_batch
from ocfl_cli.store.staging import StagingStore
from ocfl_cli.workflow import commit as wf_commit
from ocfl_cli.workflow.batch import report as batch_report
from ocfl_lib import LocalFS, Spec, StorageRoot, get_layout


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


def _invoke(runner: CliRunner, storage_root: Path, staging: Path, *args: str):
    return runner.invoke(
        app,
        ["--root", str(storage_root), "stage", "--staging-root", str(staging), *args],
    )


def _batch(
    staging: Path,
    storage_root: Path,
    *,
    target: batch_model.BatchTarget = "inventory",
    rows: tuple[BatchObject, ...],
) -> BatchFile:
    batch = BatchFile(
        batch_id=batch_state.new_batch_id(),
        target=target,
        root_path=str(storage_root.resolve()),
        category_id=641,
        created_at="2026-05-15T00:00:00Z",
        updated_at="2026-05-15T00:00:00Z",
        objects=rows,
    )
    save_batch(staging, batch)
    return batch


def _stage_dir(staging: Path, storage_root: Path, object_id: str) -> Path:
    path = StagingStore(staging).staging_path(str(storage_root.resolve()), object_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _init_storage_root(path: Path) -> Path:
    path.mkdir()
    fs = LocalFS(path)
    layout = get_layout("0002-flat-direct-storage-layout")
    StorageRoot.init(fs, fs, spec=Spec.V1_1, layout=layout)
    return path


def _events(output: str) -> list[dict]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def test_commit_batch_output_helpers_have_typed_contracts() -> None:
    object_event_hints = get_type_hints(batch_report.commit_object_event)
    summary_hints = get_type_hints(batch_report.commit_summary_event)
    summary_contract = get_type_hints(summary_hints["return"])

    assert is_typeddict(object_event_hints["return"])
    assert is_typeddict(summary_hints["return"])
    assert is_typeddict(summary_contract["counts"])
    object_contract = get_type_hints(object_event_hints["return"])
    assert set(get_args(object_contract["event"].__value__)) == {
        "object_committed",
        "object_skipped",
        "object_failed",
    }


def test_all_upgraded_batch_commits_every_object(
    runner: CliRunner,
    storage_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    committed: list[tuple[str, bool]] = []
    for object_id in ("obj:one", "obj:two"):
        _stage_dir(staging, storage_root, object_id)
    batch = _batch(
        staging,
        storage_root,
        rows=(
            BatchObject(object_id="obj:one", status="upgraded"),
            BatchObject(object_id="obj:two", status="upgraded"),
        ),
    )

    def fake_commit_ots(**kwargs) -> None:
        committed.append((kwargs["object_id"], kwargs["allow_missing_metadata"]))
        shutil.rmtree(kwargs["staging_dir"])

    monkeypatch.setattr(wf_commit, "commit_ots", fake_commit_ots)

    result = _invoke_json(
        runner, storage_root, staging, "commit", "--batch", batch.batch_id
    )

    assert result.exit_code == 0, result.output
    assert committed == [("obj:one", False), ("obj:two", False)]
    assert _events(result.output) == [
        {
            "event": "object_committed",
            "batch_id": batch.batch_id,
            "target": "inventory",
            "object_id": "obj:one",
            "reason": None,
            "message": None,
        },
        {
            "event": "object_committed",
            "batch_id": batch.batch_id,
            "target": "inventory",
            "object_id": "obj:two",
            "reason": None,
            "message": None,
        },
        {
            "event": "summary",
            "batch_id": batch.batch_id,
            "target": "inventory",
            "category_id": 641,
            "root_path": str(storage_root.resolve()),
            "counts": {"committed": 2, "skipped": 0, "failed": 0, "total": 2},
        },
    ]


def test_mixed_batch_commits_upgraded_and_skips_other_rows(
    runner: CliRunner,
    storage_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    for object_id in ("obj:ready", "obj:failed", "obj:skipped"):
        _stage_dir(staging, storage_root, object_id)
    batch = _batch(
        staging,
        storage_root,
        rows=(
            BatchObject(object_id="obj:ready", status="upgraded"),
            BatchObject(
                object_id="obj:failed",
                status="failed",
                skipped_reason="attestation_mismatch",
            ),
            BatchObject(
                object_id="obj:skipped",
                status="skipped",
                skipped_reason="noop",
            ),
        ),
    )
    monkeypatch.setattr(
        wf_commit,
        "commit_ots",
        lambda **kwargs: shutil.rmtree(kwargs["staging_dir"]),
    )

    result = _invoke_json(
        runner, storage_root, staging, "commit", "--batch", batch.batch_id
    )

    assert result.exit_code == 0, result.output
    assert _events(result.output) == [
        {
            "event": "object_committed",
            "batch_id": batch.batch_id,
            "target": "inventory",
            "object_id": "obj:ready",
            "reason": None,
            "message": None,
        },
        {
            "event": "object_skipped",
            "batch_id": batch.batch_id,
            "target": "inventory",
            "object_id": "obj:failed",
            "reason": "attestation_mismatch",
            "message": None,
        },
        {
            "event": "object_skipped",
            "batch_id": batch.batch_id,
            "target": "inventory",
            "object_id": "obj:skipped",
            "reason": "noop",
            "message": None,
        },
        {
            "event": "summary",
            "batch_id": batch.batch_id,
            "target": "inventory",
            "category_id": 641,
            "root_path": str(storage_root.resolve()),
            "counts": {"committed": 1, "skipped": 2, "failed": 0, "total": 3},
        },
    ]


def test_batch_commit_continues_after_failure_and_exits_one(
    runner: CliRunner,
    storage_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    for object_id in ("obj:stale", "obj:next"):
        _stage_dir(staging, storage_root, object_id)
    batch = _batch(
        staging,
        storage_root,
        rows=(
            BatchObject(object_id="obj:stale", status="upgraded"),
            BatchObject(object_id="obj:next", status="upgraded"),
        ),
    )
    calls: list[str] = []

    def fake_commit_ots(**kwargs) -> None:
        calls.append(kwargs["object_id"])
        if kwargs["object_id"] == "obj:stale":
            raise AttestationMismatchError("plan digest mismatch")
        shutil.rmtree(kwargs["staging_dir"])

    monkeypatch.setattr(wf_commit, "commit_ots", fake_commit_ots)

    result = _invoke_json(
        runner, storage_root, staging, "commit", "--batch", batch.batch_id
    )

    assert result.exit_code == 1, result.output
    assert calls == ["obj:stale", "obj:next"]
    assert _events(result.output) == [
        {
            "event": "object_failed",
            "batch_id": batch.batch_id,
            "target": "inventory",
            "object_id": "obj:stale",
            "reason": "attestation_mismatch",
            "message": "plan digest mismatch",
        },
        {
            "event": "object_committed",
            "batch_id": batch.batch_id,
            "target": "inventory",
            "object_id": "obj:next",
            "reason": None,
            "message": None,
        },
        {
            "event": "summary",
            "batch_id": batch.batch_id,
            "target": "inventory",
            "category_id": 641,
            "root_path": str(storage_root.resolve()),
            "counts": {"committed": 1, "skipped": 0, "failed": 1, "total": 2},
        },
    ]


def test_resume_after_partial_commit_skips_missing_staging(
    runner: CliRunner,
    storage_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    _stage_dir(staging, storage_root, "obj:remaining")
    batch = _batch(
        staging,
        storage_root,
        rows=(
            BatchObject(object_id="obj:done", status="upgraded"),
            BatchObject(object_id="obj:remaining", status="upgraded"),
        ),
    )
    monkeypatch.setattr(
        wf_commit,
        "commit_ots",
        lambda **kwargs: shutil.rmtree(kwargs["staging_dir"]),
    )

    result = _invoke_json(
        runner, storage_root, staging, "commit", "--batch", batch.batch_id
    )

    assert result.exit_code == 0, result.output
    assert _events(result.output) == [
        {
            "event": "object_skipped",
            "batch_id": batch.batch_id,
            "target": "inventory",
            "object_id": "obj:done",
            "reason": "committed",
            "message": None,
        },
        {
            "event": "object_committed",
            "batch_id": batch.batch_id,
            "target": "inventory",
            "object_id": "obj:remaining",
            "reason": None,
            "message": None,
        },
        {
            "event": "summary",
            "batch_id": batch.batch_id,
            "target": "inventory",
            "category_id": 641,
            "root_path": str(storage_root.resolve()),
            "counts": {"committed": 1, "skipped": 1, "failed": 0, "total": 2},
        },
    ]


def test_content_target_batch_is_rejected_before_commit_attempt(
    runner: CliRunner,
    storage_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    _stage_dir(staging, storage_root, "obj:content")
    batch = _batch(
        staging,
        storage_root,
        target="content",
        rows=(BatchObject(object_id="obj:content", status="upgraded"),),
    )
    monkeypatch.setattr(
        wf_commit,
        "commit_ots",
        lambda **kwargs: pytest.fail("content batch attempted object commit"),
    )

    result = _invoke(runner, storage_root, staging, "commit", "--batch", batch.batch_id)

    assert result.exit_code == 2
    assert "commit batches must target inventory; got content" in result.output


def test_batch_commit_rejects_wrong_archive_root_before_commit_attempt(
    runner: CliRunner,
    storage_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    other_root = _init_storage_root(tmp_path / "other-root")
    _stage_dir(staging, storage_root, "obj:wrong-root")
    batch = _batch(
        staging,
        storage_root,
        rows=(BatchObject(object_id="obj:wrong-root", status="upgraded"),),
    )
    monkeypatch.setattr(
        wf_commit,
        "commit_ots",
        lambda **kwargs: pytest.fail("wrong-root batch attempted object commit"),
    )

    result = _invoke(runner, other_root, staging, "commit", "--batch", batch.batch_id)

    assert result.exit_code == 2
    assert f"batch {batch.batch_id} belongs to root" in result.output
    assert str(storage_root.resolve()) in result.output
    assert str(other_root.resolve()) in result.output


def test_batch_commit_does_not_require_batch_driver_lease(
    runner: CliRunner,
    storage_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    _stage_dir(staging, storage_root, "obj:leased")
    batch = _batch(
        staging,
        storage_root,
        rows=(BatchObject(object_id="obj:leased", status="upgraded"),),
    )
    monkeypatch.setattr(
        wf_commit,
        "commit_ots",
        lambda **kwargs: shutil.rmtree(kwargs["staging_dir"]),
    )

    with batch_state.with_driver_lease(
        staging,
        batch.batch_id,
        command=(
            "stage",
            "timestamp",
            "batch",
            "inventory",
            "--batch",
            batch.batch_id,
            "--retry",
        ),
        heartbeat_interval=None,
    ):
        result = _invoke(
            runner, storage_root, staging, "commit", "--batch", batch.batch_id
        )

    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((), "exactly one of OBJECT_ID or --batch is required"),
        (
            ("obj:one", "--batch", "018f63de-0000-7000-8000-000000000001"),
            "exactly one of OBJECT_ID or --batch is required",
        ),
        (
            (
                "--batch",
                "018f63de-0000-7000-8000-000000000001",
                "--allow-missing-metadata",
            ),
            "--allow-missing-metadata is not supported with --batch; "
            "commit those objects individually",
        ),
    ],
)
def test_batch_commit_usage_errors(
    runner: CliRunner,
    storage_root: Path,
    tmp_path: Path,
    args: tuple[str, ...],
    message: str,
) -> None:
    result = _invoke(runner, storage_root, tmp_path / "staging", "commit", *args)

    assert result.exit_code == 2
    assert message in result.output


def test_snapshot_row_invalidated_mid_run_is_reported_as_failed(
    runner: CliRunner,
    storage_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    for object_id in ("obj:invalidated", "obj:after"):
        _stage_dir(staging, storage_root, object_id)
    batch = _batch(
        staging,
        storage_root,
        rows=(
            BatchObject(object_id="obj:invalidated", status="upgraded"),
            BatchObject(object_id="obj:after", status="upgraded"),
        ),
    )
    calls: list[str] = []

    def fake_commit_ots(**kwargs) -> None:
        calls.append(kwargs["object_id"])
        current = batch_state.load_batch(staging, batch.batch_id)
        if kwargs["object_id"] == "obj:invalidated":
            batch_state.save_batch(
                staging,
                replace(
                    current,
                    objects=(
                        BatchObject(object_id="obj:invalidated", status="prepared"),
                        current.objects[1],
                    ),
                ),
            )
            raise PhaseError("inventory timestamp plan missing or stale")
        shutil.rmtree(kwargs["staging_dir"])

    monkeypatch.setattr(wf_commit, "commit_ots", fake_commit_ots)

    result = _invoke_json(
        runner, storage_root, staging, "commit", "--batch", batch.batch_id
    )

    assert result.exit_code == 1, result.output
    assert calls == ["obj:invalidated", "obj:after"]
    assert _events(result.output)[0] == {
        "event": "object_failed",
        "batch_id": batch.batch_id,
        "target": "inventory",
        "object_id": "obj:invalidated",
        "reason": "phase_error",
        "message": "inventory timestamp plan missing or stale",
    }
    assert _events(result.output)[-1]["counts"] == {
        "committed": 1,
        "skipped": 0,
        "failed": 1,
        "total": 2,
    }
