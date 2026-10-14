import fcntl
import hashlib
import json
import multiprocessing
import os
import queue
import time
from pathlib import Path
from typing import cast, get_args, get_origin, get_type_hints, is_typeddict

import pytest
from fakes import (
    FakeBitcoinRPC,
    FakeSimpleProof,
    InMemoryHashBlockStream,
    RecordingSleeper,
)
from fakes.locks import held_flock
from msgspec.structs import replace
from ots_helpers import (
    MIN_CONFS,
    UNRELATED_DIGEST,
    bury,
    end_chain_below,
    inject_fake_btc,
    inject_fake_sp,
    ots_for,
    register_attested,
    write_config,
)
from runner_helpers import runner_obj, stage_args
from typer.testing import CliRunner

from ocfl_cli.errors import (
    AttestationMismatchError,
    SimpleProofTransient,
    UsageError,
)
from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app
from ocfl_cli.model import batch as batch_model
from ocfl_cli.model.batch import (
    BatchFile,
    BatchObject,
    BatchObjectStatus,
    BatchSubmission,
    BatchVerifyJob,
)
from ocfl_cli.model.metadata import StagedMetadata, StagedUser
from ocfl_cli.model.ots_state import (
    OtsState,
    Proof,
    StagedInventory,
    Submission,
    VerifyJob,
)
from ocfl_cli.model.phase import Phase, TimestampTarget
from ocfl_cli.model.simpleproof import (
    JobStatus,
    SimpleProofDownloadResult,
    SubmitResult,
)
from ocfl_cli.stage import batch as stage_batch
from ocfl_cli.store import batch as batch_state
from ocfl_cli.store.batch import batch_file_path, new_batch_id, save_batch
from ocfl_cli.store.ots_state import load_ots, ots_cache_path, save_ots
from ocfl_cli.store.staging import StagingStore
from ocfl_cli.workflow.batch import reasons as batch_reasons
from ocfl_cli.workflow.batch import report as batch_report
from ocfl_cli.workflow.batch import upgrade as batch_upgrade
from ocfl_lib import (
    BitcoinRpcTransientError,
)

_HEIGHT_JUMP = 5


def _inv_submission(digest: str, batch_id: str, record_id: str | None) -> Submission:
    return Submission(
        submitted_at="2026-05-12T10:00:00+00:00",
        digests=(digest,),
        status="submitted" if record_id is not None else "submitting",
        target=TimestampTarget.INVENTORY,
        batch_id=batch_id,
        record_ids=(record_id,),
    )


def test_batch_output_helpers_have_typed_contracts() -> None:
    status_hints = get_type_hints(batch_report.batch_status_dict)
    object_event_hints = get_type_hints(batch_report.batch_object_event)
    digest_rows_hints = get_type_hints(batch_report.verbose_digest_rows)
    status_contract = get_type_hints(status_hints["return"])

    assert is_typeddict(status_hints["return"])
    assert is_typeddict(object_event_hints["return"])
    assert get_origin(digest_rows_hints["return"]) is list
    assert is_typeddict(get_args(digest_rows_hints["return"])[0])
    assert is_typeddict(get_args(status_contract["submissions"])[0])
    assert is_typeddict(get_args(status_contract["verify_jobs"])[0])


def test_fixed_batch_reasons_are_canonicalized_through_helper() -> None:
    assert set(batch_reasons.FIXED_BATCH_REASONS) == set(
        get_args(batch_reasons.FixedBatchReason.__value__)
    )
    assert "sp_partial_results" in batch_reasons.FIXED_BATCH_REASONS
    assert [
        batch_reasons.fixed_batch_reason(reason)
        for reason in batch_reasons.FIXED_BATCH_REASONS
    ] == list(batch_reasons.FIXED_BATCH_REASONS)


@pytest.mark.parametrize("status", ["PENDING", "PROCESSING"])
def test_batch_verify_active_statuses_include_simpleproof_v2_states(
    status: batch_model.BatchVerifyJobStatus,
) -> None:
    digest = "a" * 64
    batch = BatchFile(
        batch_id="batch-v2-statuses",
        target=TimestampTarget.CONTENT,
        root_path="/repo",
        category_id=641,
        created_at="2026-05-12T10:00:00Z",
        updated_at="2026-05-12T10:00:00Z",
        objects=(
            BatchObject(
                object_id="obj:v2-statuses",
                status="submitted",
                digests=(digest,),
            ),
        ),
        verify_jobs=(
            BatchVerifyJob(
                job_id=f"job-{status.lower()}",
                digests=(digest,),
                status=status,
                submitted_at="2026-05-12T10:00:00Z",
            ),
        ),
    )

    assert batch_report.verify_job_covers(batch, batch.objects[0])
    assert batch_upgrade.covered_verify_digests(batch) == {digest}


def _assert_z_timestamp(value: str) -> None:
    assert value.endswith("Z")
    assert "+00:00" not in value


class MismatchedRecordIdsFake(FakeSimpleProof):
    def submit_hashes(self, digests: list[str], category_id: int) -> SubmitResult:
        super().submit_hashes(digests, category_id)
        return SubmitResult(digests=tuple(digests), record_ids=tuple())


class TransientSubmitFake(FakeSimpleProof):
    calls: int = 0

    def submit_hashes(self, digests: list[str], category_id: int) -> SubmitResult:
        self.calls += 1
        raise SimpleProofTransient("SimpleProof HTTP 503")


class TransientPollFake(FakeSimpleProof):
    def poll_job(self, job_id: str):
        raise SimpleProofTransient("SimpleProof HTTP 503")


class ExpiredPollFake(FakeSimpleProof):
    def poll_job(self, job_id: str) -> JobStatus:
        job = self._jobs[job_id]
        return JobStatus(
            job_id=job.job_id,
            status="EXPIRED",
            results_url=job.results_url,
        )


class MismatchDownloadFake(FakeSimpleProof):
    def download_results(
        self, results_url: str, expected_digests: set[str]
    ) -> SimpleProofDownloadResult:
        raise AttestationMismatchError("unexpected digest from SP")


class TransientBitcoinRPC(FakeBitcoinRPC):
    def getblockchaininfo(self):
        raise BitcoinRpcTransientError("Bitcoin RPC timeout")


class SecondSubmitTransientFake(FakeSimpleProof):
    calls: int = 0

    def submit_hashes(self, digests: list[str], category_id: int) -> SubmitResult:
        self.calls += 1
        if self.calls == 2:
            raise SimpleProofTransient("SimpleProof HTTP 503")
        return super().submit_hashes(digests, category_id)


class BlockingSubmitFake(FakeSimpleProof):
    def __init__(self, marker: Path, release: Path) -> None:
        super().__init__()
        self.marker = marker
        self.release = release

    def submit_hashes(self, digests: list[str], category_id: int) -> SubmitResult:
        self.marker.write_text("ready")
        deadline = time.monotonic() + 10
        while not self.release.exists():
            if time.monotonic() > deadline:
                raise TimeoutError(f"timed out waiting for {self.release}")
            time.sleep(0.01)
        return super().submit_hashes(digests, category_id)


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


def _inject_fake_zmq(runner: CliRunner, stream) -> None:

    runner_obj(runner)["block_stream_factory"] = lambda *a, **kw: stream


def _run_batch_command_process(
    root: str,
    staging: str,
    args: tuple[str, ...],
    marker: str | None,
    release: str | None,
    out: multiprocessing.Queue,
) -> None:
    runner = CliRunner()
    obj = {}
    if marker is not None and release is not None:
        obj["sp_client_factory"] = lambda cfg: BlockingSubmitFake(
            Path(marker), Path(release)
        )
    else:
        obj["sp_client_factory"] = lambda cfg: FakeSimpleProof()
    result = runner.invoke(app, stage_args(Path(root), Path(staging), *args), obj=obj)
    diagnostic = result.output
    if result.exception is not None:
        diagnostic = f"{diagnostic}{result.exception!r}"
    out.put((result.exit_code, diagnostic))


def _start_batch_process(
    root: Path,
    staging: Path,
    args: tuple[str, ...],
    *,
    marker: Path | None = None,
    release: Path | None = None,
):
    ctx = multiprocessing.get_context("spawn")
    out = ctx.Queue()
    process = ctx.Process(
        target=_run_batch_command_process,
        args=(
            str(root),
            str(staging),
            args,
            str(marker) if marker is not None else None,
            str(release) if release is not None else None,
            out,
        ),
    )
    process.start()
    return process, out


def _wait_for_path(path: Path, *, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {path}")
        time.sleep(0.01)


def _process_result(
    process: multiprocessing.Process, out: multiprocessing.Queue
) -> tuple[int, str]:
    process.join(timeout=10)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        raise AssertionError("child process did not exit")
    try:
        return out.get_nowait()
    except queue.Empty as e:
        raise AssertionError("child process produced no result") from e


def _new_ots_with_files(
    runner: CliRunner,
    root: Path,
    staging: Path,
    object_id: str,
    *files: tuple[str, bytes],
) -> None:
    result = runner.invoke(app, stage_args(root, staging, "new", object_id, "--ots"))
    assert result.exit_code == ExitCode.SUCCESS, result.output
    for name, content in files:
        src = staging.parent / f"{object_id.replace(':', '_')}-{name}"
        src.write_bytes(content)
        result = runner.invoke(
            app, stage_args(root, staging, "add", object_id, str(src))
        )
        assert result.exit_code == ExitCode.SUCCESS, result.output


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


def test_batch_content_new_batch_submits_one_union_call(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner,
        empty_sha256_root,
        staging,
        "obj:one",
        ("a.txt", b"alpha"),
        ("b.txt", b"bravo"),
    )
    _new_ots_with_files(
        runner,
        empty_sha256_root,
        staging,
        "obj:two",
        ("c.txt", b"charlie"),
    )

    result = runner.invoke(
        app,
        stage_args(
            empty_sha256_root,
            staging,
            "batch",
            "create",
            "content",
            "obj:one",
            "obj:two",
        ),
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "batch created:" in result.output
    assert len(sp_fake._submissions) == 1
    assert len(sp_fake._submissions[0].digests) == 3
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))
    batch = batch_state.load_batch(staging, batch_id)
    assert batch.target == "content"
    assert batch.category_id == 641
    assert [row.status for row in batch.objects] == ["submitted", "submitted"]
    assert len(batch.submissions) == 1
    assert batch.submissions[0].record_ids == sp_fake._submissions[0].record_ids
    assert batch.submissions[0].submitted_at is not None
    _assert_z_timestamp(batch.submissions[0].submitted_at)
    _assert_z_timestamp(batch.updated_at)

    store = StagingStore(staging)
    for object_id, expected_count in (("obj:one", 2), ("obj:two", 1)):
        state = load_ots(
            store.staging_path(str(empty_sha256_root.resolve()), object_id)
        )
        assert state.phase is Phase.CONTENT_PENDING
        assert len(state.submissions) == 1
        sub = state.submissions[0]
        assert sub.batch_id == batch_id
        assert sub.status == "submitted"
        assert sub.record_ids is not None
        assert len([rid for rid in sub.record_ids if rid is not None]) == expected_count


def test_batch_content_new_batch_json_emits_batch_id_before_submit(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(runner, empty_sha256_root, staging, "obj:json", ("a.txt", b"a"))

    result = runner.invoke(
        app,
        [
            "--root",
            str(empty_sha256_root),
            "--json",
            "stage",
            "--staging-root",
            str(staging),
            "batch",
            "create",
            "content",
            "obj:json",
        ],
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    events = [json.loads(line) for line in result.output.splitlines()]
    data = events[0]
    assert data["event"] == "batch_created"
    assert data["target"] == "content"
    assert data["category_id"] == 641
    assert data["batch_id"]
    _assert_z_timestamp(data["created_at"])

    batch = batch_state.load_batch(staging, data["batch_id"])
    _assert_z_timestamp(batch.created_at)
    _assert_z_timestamp(batch.updated_at)


def test_batch_content_new_batch_json_is_ndjson_driving_output(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(runner, empty_sha256_root, staging, "obj:json", ("a.txt", b"a"))

    result = _invoke_json(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "create",
        "content",
        "obj:json",
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    events = [json.loads(line) for line in result.output.splitlines()]
    assert [event["event"] for event in events] == [
        "batch_created",
        "object_prepared",
        "object_submitted",
        "summary",
    ]
    assert all(isinstance(line, dict) for line in events)
    assert events[1]["object_id"] == "obj:json"
    assert events[-1]["counts"]["submitted"] == 1


def test_batch_content_bare_batch_mirrors_persisted_chunk_without_repost(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner, empty_sha256_root, staging, "obj:resume", ("a.txt", b"a")
    )

    create = runner.invoke(
        app,
        stage_args(
            empty_sha256_root,
            staging,
            "batch",
            "create",
            "content",
            "obj:resume",
        ),
    )
    assert create.exit_code == ExitCode.SUCCESS, create.output
    assert len(sp_fake._submissions) == 1
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))

    store = StagingStore(staging)
    staging_dir = store.staging_path(str(empty_sha256_root.resolve()), "obj:resume")
    state = load_ots(staging_dir)
    save_ots(
        staging_dir,
        replace(
            state,
            phase=Phase.DRAFTING,
            submissions=(
                replace(state.submissions[0], status="submitting", record_ids=None),
            ),
        ),
    )

    resume = runner.invoke(
        app,
        stage_args(
            empty_sha256_root,
            staging,
            "batch",
            "submit",
            batch_id,
        ),
    )

    assert resume.exit_code == ExitCode.SUCCESS, resume.output
    assert len(sp_fake._submissions) == 1
    resumed_state = load_ots(staging_dir)
    assert resumed_state.submissions[0].status == "submitted"
    assert resumed_state.submissions[0].record_ids == sp_fake._submissions[0].record_ids


def test_batch_inventory_new_batch_submits_and_mirrors_record_id(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(runner, empty_sha256_root, staging, "obj:inv", ("a.txt", b"a"))

    store = StagingStore(staging)
    staging_dir = store.staging_path(str(empty_sha256_root.resolve()), "obj:inv")
    state = load_ots(staging_dir)
    digest = hashlib.sha256(b"a").hexdigest()
    save_ots(
        staging_dir,
        replace(
            state,
            phase=Phase.CONTENT_UPGRADED,
            proofs={digest: Proof(target=TimestampTarget.CONTENT, verified=True)},
        ),
    )

    result = runner.invoke(
        app,
        stage_args(
            empty_sha256_root,
            staging,
            "batch",
            "create",
            "inventory",
            "obj:inv",
            "--allow-missing-metadata",
        ),
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert len(sp_fake._submissions) == 1
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))
    batch = batch_state.load_batch(staging, batch_id)
    assert batch.target == "inventory"
    assert batch.objects[0].status == "submitted"
    assert batch.submissions[0].record_ids == ("rec-1",)
    updated = load_ots(staging_dir)
    assert updated.phase is Phase.INVENTORY_PENDING
    assert updated.inventory is not None
    assert batch.objects[0].digests == (updated.inventory.digest,)
    assert batch.submissions[0].digests == (updated.inventory.digest,)
    assert sp_fake._submissions[0].digests == (updated.inventory.digest,)
    (sub,) = [s for s in updated.submissions if s.target == "inventory"]
    assert (sub.status, sub.batch_id, sub.record_ids) == (
        "submitted",
        batch_id,
        ("rec-1",),
    )


def test_batch_inventory_same_object_skips_until_content_upgraded(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner, empty_sha256_root, staging, "obj:overlap", ("a.txt", b"a")
    )
    content = runner.invoke(
        app,
        stage_args(
            empty_sha256_root,
            staging,
            "batch",
            "create",
            "content",
            "obj:overlap",
        ),
    )
    assert content.exit_code == ExitCode.SUCCESS, content.output

    inventory = runner.invoke(
        app,
        stage_args(
            empty_sha256_root,
            staging,
            "batch",
            "create",
            "inventory",
            "obj:overlap",
            "--allow-missing-metadata",
        ),
    )

    assert inventory.exit_code == ExitCode.SUCCESS, inventory.output
    batches = [
        batch_state.load_batch(staging, p.stem)
        for p in (staging / "batches").glob("*.json")
    ]
    inventory_batch = next(batch for batch in batches if batch.target == "inventory")
    assert inventory_batch.objects[0].status == "skipped"
    assert inventory_batch.objects[0].skipped_reason == "content_not_upgraded"


def test_batch_inventory_after_content_upgraded_ignores_content_batch_owner(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner, empty_sha256_root, staging, "obj:after-content", ("a.txt", b"a")
    )
    content = runner.invoke(
        app,
        stage_args(
            empty_sha256_root,
            staging,
            "batch",
            "create",
            "content",
            "obj:after-content",
        ),
    )
    assert content.exit_code == ExitCode.SUCCESS, content.output
    store = StagingStore(staging)
    staging_dir = store.staging_path(
        str(empty_sha256_root.resolve()), "obj:after-content"
    )
    state = load_ots(staging_dir)
    digest = hashlib.sha256(b"a").hexdigest()
    save_ots(
        staging_dir,
        replace(
            state,
            phase=Phase.CONTENT_UPGRADED,
            proofs={digest: Proof(target=TimestampTarget.CONTENT, verified=True)},
        ),
    )

    inventory = runner.invoke(
        app,
        stage_args(
            empty_sha256_root,
            staging,
            "batch",
            "create",
            "inventory",
            "obj:after-content",
            "--allow-missing-metadata",
        ),
    )

    assert inventory.exit_code == ExitCode.SUCCESS, inventory.output
    batches = [
        batch_state.load_batch(staging, p.stem)
        for p in (staging / "batches").glob("*.json")
    ]
    inventory_batch = next(batch for batch in batches if batch.target == "inventory")
    assert inventory_batch.objects[0].status == "submitted"
    updated = load_ots(staging_dir)
    assert updated.inventory is not None
    (sub,) = [s for s in updated.submissions if s.target == "inventory"]
    assert sub.batch_id == inventory_batch.batch_id
    assert sub.record_ids is not None and None not in sub.record_ids


def test_batch_content_shape_mismatch_fails_chunk_closed(
    runner: CliRunner,
    empty_sha256_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = MismatchedRecordIdsFake()
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner,
        empty_sha256_root,
        staging,
        "obj:mismatch",
        ("a.txt", b"a"),
        ("b.txt", b"b"),
    )

    result = runner.invoke(
        app,
        stage_args(
            empty_sha256_root,
            staging,
            "batch",
            "create",
            "content",
            "obj:mismatch",
        ),
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))
    batch = batch_state.load_batch(staging, batch_id)
    assert batch.submissions[0].status == "failed"
    assert batch.submissions[0].last_error == "missing_record_ids"
    assert batch.objects[0].status == "prepared"
    assert batch.objects[0].last_error == "missing_record_ids"
    state = load_ots(
        StagingStore(staging).staging_path(
            str(empty_sha256_root.resolve()), "obj:mismatch"
        )
    )
    assert state.submissions[0].status == "submitting"
    assert state.submissions[0].record_ids == (None, None)


def test_batch_content_bare_batch_does_not_repost_failed_chunk(
    runner: CliRunner,
    empty_sha256_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = MismatchedRecordIdsFake()
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner, empty_sha256_root, staging, "obj:failed", ("a.txt", b"a")
    )
    create = runner.invoke(
        app,
        stage_args(
            empty_sha256_root,
            staging,
            "batch",
            "create",
            "content",
            "obj:failed",
        ),
    )
    assert create.exit_code == ExitCode.SUCCESS, create.output
    assert len(fake._submissions) == 1
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))

    resume = runner.invoke(
        app,
        stage_args(
            empty_sha256_root,
            staging,
            "batch",
            "submit",
            batch_id,
        ),
    )

    assert resume.exit_code == ExitCode.SUCCESS, resume.output
    assert len(fake._submissions) == 1
    batch = batch_state.load_batch(staging, batch_id)
    assert batch.submissions[0].status == "failed"
    assert batch.objects[0].last_error == "missing_record_ids"


def test_batch_content_new_batch_acquires_lease_before_file_is_visible(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner, empty_sha256_root, staging, "obj:lease", ("a.txt", b"a")
    )
    original_flock = fcntl.flock
    observed_clean = []

    def checked_flock(fd, operation):
        path = os.readlink(f"/proc/self/fd/{fd}")
        if path.endswith(".lock") and Path(path).parent.name == "batches":
            observed_clean.append(not list((staging / "batches").glob("*.json")))
        return original_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", checked_flock)

    result = runner.invoke(
        app,
        stage_args(
            empty_sha256_root,
            staging,
            "batch",
            "create",
            "content",
            "obj:lease",
        ),
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert observed_clean == [True], "lease (batch flock) precedes the batch file"


def test_batch_content_competing_batch_driver_refuses_live_lease(
    runner: CliRunner,
    empty_sha256_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner, empty_sha256_root, staging, "obj:driver", ("a.txt", b"a")
    )
    marker = tmp_path / "driver-ready"
    release = tmp_path / "driver-release"
    first, first_out = _start_batch_process(
        empty_sha256_root,
        staging,
        ("batch", "create", "content", "obj:driver"),
        marker=marker,
        release=release,
    )
    _wait_for_path(marker)
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))

    second, second_out = _start_batch_process(
        empty_sha256_root,
        staging,
        ("batch", "submit", batch_id),
    )
    second_code, second_output = _process_result(second, second_out)
    release.write_text("go")
    first_code, first_output = _process_result(first, first_out)

    assert first_code == ExitCode.SUCCESS, first_output
    assert second_code == ExitCode.LOCK_TIMEOUT
    assert f"could not acquire batch {batch_id}" in second_output
    batch = batch_state.load_batch(staging, batch_id)
    assert batch.submissions[0].status == "submitted"


def test_batch_content_competing_same_object_batch_skips_in_batch(
    runner: CliRunner,
    empty_sha256_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    staging = tmp_path / "staging"
    _new_ots_with_files(runner, empty_sha256_root, staging, "obj:same", ("a.txt", b"a"))
    marker = tmp_path / "same-ready"
    release = tmp_path / "same-release"
    first, first_out = _start_batch_process(
        empty_sha256_root,
        staging,
        ("batch", "create", "content", "obj:same"),
        marker=marker,
        release=release,
    )
    _wait_for_path(marker)

    second, second_out = _start_batch_process(
        empty_sha256_root,
        staging,
        ("batch", "create", "content", "obj:same"),
    )
    second_code, second_output = _process_result(second, second_out)
    release.write_text("go")
    first_code, first_output = _process_result(first, first_out)

    assert first_code == ExitCode.SUCCESS, first_output
    assert second_code == ExitCode.SUCCESS, second_output
    batches = [
        batch_state.load_batch(staging, p.stem)
        for p in (staging / "batches").glob("*.json")
    ]
    skipped = [
        batch
        for batch in batches
        if batch.objects and batch.objects[0].status == "skipped"
    ]
    assert len(skipped) == 1
    assert skipped[0].objects[0].skipped_reason is not None
    assert skipped[0].objects[0].skipped_reason.startswith("in_batch=")


def test_batch_content_competing_cross_object_batches_both_submit(
    runner: CliRunner,
    empty_sha256_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner, empty_sha256_root, staging, "obj:left", ("left.txt", b"left")
    )
    _new_ots_with_files(
        runner, empty_sha256_root, staging, "obj:right", ("right.txt", b"right")
    )
    left_marker = tmp_path / "left-ready"
    left_release = tmp_path / "left-release"
    right_marker = tmp_path / "right-ready"
    right_release = tmp_path / "right-release"
    left, left_out = _start_batch_process(
        empty_sha256_root,
        staging,
        ("batch", "create", "content", "obj:left"),
        marker=left_marker,
        release=left_release,
    )
    right, right_out = _start_batch_process(
        empty_sha256_root,
        staging,
        ("batch", "create", "content", "obj:right"),
        marker=right_marker,
        release=right_release,
    )
    _wait_for_path(left_marker)
    _wait_for_path(right_marker)
    left_release.write_text("go")
    right_release.write_text("go")
    left_code, left_output = _process_result(left, left_out)
    right_code, right_output = _process_result(right, right_out)

    assert left_code == ExitCode.SUCCESS, left_output
    assert right_code == ExitCode.SUCCESS, right_output
    batches = [
        batch_state.load_batch(staging, p.stem)
        for p in (staging / "batches").glob("*.json")
    ]
    assert sorted(row.object_id for batch in batches for row in batch.objects) == [
        "obj:left",
        "obj:right",
    ]
    assert all(row.status == "submitted" for batch in batches for row in batch.objects)


def test_batch_content_recovers_object_intent_without_batch_row(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner, empty_sha256_root, staging, "obj:orphan", ("a.txt", b"a")
    )
    batch_id = new_batch_id()
    digest = hashlib.sha256(b"a").hexdigest()
    store = StagingStore(staging)
    staging_dir = store.staging_path(str(empty_sha256_root.resolve()), "obj:orphan")
    state = load_ots(staging_dir)
    save_ots(
        staging_dir,
        replace(
            state,
            pending_digests=(digest,),
            submissions=(
                Submission(
                    submitted_at="2026-05-12T10:00:00Z",
                    digests=(digest,),
                    status="submitting",
                    target=TimestampTarget.CONTENT,
                    batch_id=batch_id,
                    record_ids=(None,),
                ),
            ),
        ),
    )
    save_batch(
        staging,
        BatchFile(
            batch_id=batch_id,
            target=TimestampTarget.CONTENT,
            root_path=str(empty_sha256_root.resolve()),
            category_id=641,
            created_at="2026-05-12T10:00:00Z",
            updated_at="2026-05-12T10:00:00Z",
        ),
    )
    batch_lock = str(staging / "batches" / f"{batch_id}.lock")
    events: list[str] = []
    original_flock = fcntl.flock

    def recording_flock(fd, operation):
        path = os.readlink(f"/proc/self/fd/{fd}")
        if operation & fcntl.LOCK_EX:
            events.append("batch" if path == batch_lock else "object")
        return original_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", recording_flock)

    result = runner.invoke(
        app,
        stage_args(
            empty_sha256_root,
            staging,
            "batch",
            "submit",
            batch_id,
        ),
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert events[0] == "batch" and events.count("batch") == 1, (
        f"batch lock must be taken first, once, never under an object lock: {events}"
    )
    assert "object" in events
    batch = batch_state.load_batch(staging, batch_id)
    assert batch.objects[0].object_id == "obj:orphan"
    assert batch.objects[0].status == "submitted"
    assert len(sp_fake._submissions) == 1
    updated = load_ots(staging_dir)
    assert updated.submissions[0].record_ids == ("rec-1",)


def test_batch_content_resume_empty_after_revalidation_skips_post(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner, empty_sha256_root, staging, "obj:empty", ("a.txt", b"a")
    )
    create = runner.invoke(
        app,
        stage_args(
            empty_sha256_root,
            staging,
            "batch",
            "create",
            "content",
            "obj:empty",
        ),
    )
    assert create.exit_code == ExitCode.SUCCESS, create.output
    assert len(sp_fake._submissions) == 1
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))
    batch = batch_state.load_batch(staging, batch_id)
    batch_state.save_batch(
        staging,
        replace(
            batch,
            objects=(replace(batch.objects[0], status="prepared"),),
            submissions=(
                replace(batch.submissions[0], status="pending", record_ids=None),
            ),
        ),
    )

    resume = runner.invoke(
        app,
        stage_args(
            empty_sha256_root,
            staging,
            "batch",
            "submit",
            batch_id,
        ),
    )

    assert resume.exit_code == ExitCode.SUCCESS, resume.output
    assert len(sp_fake._submissions) == 1
    resumed = batch_state.load_batch(staging, batch_id)
    assert resumed.submissions[0].status == "submitted"
    assert resumed.submissions[0].digests == ()
    assert resumed.submissions[0].record_ids == ()


def test_batch_content_chunked_multi_chunk_object_completes(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, sp_fake)
    sp_fake.submit_chunk_size = 2
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner,
        empty_sha256_root,
        staging,
        "obj:chunked",
        ("a.txt", b"a"),
        ("b.txt", b"b"),
        ("c.txt", b"c"),
        ("d.txt", b"d"),
        ("e.txt", b"e"),
    )

    result = runner.invoke(
        app,
        stage_args(
            empty_sha256_root,
            staging,
            "batch",
            "create",
            "content",
            "obj:chunked",
        ),
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert [len(call.digests) for call in sp_fake._submissions] == [2, 2, 1]
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))
    batch = batch_state.load_batch(staging, batch_id)
    assert [len(chunk.digests) for chunk in batch.submissions] == [2, 2, 1]
    assert all(chunk.status == "submitted" for chunk in batch.submissions)
    state = load_ots(
        StagingStore(staging).staging_path(
            str(empty_sha256_root.resolve()), "obj:chunked"
        )
    )
    assert state.phase is Phase.CONTENT_PENDING
    assert state.submissions[0].status == "submitted"
    assert state.submissions[0].record_ids is not None
    assert len(state.submissions[0].record_ids) == 5


def test_batch_content_transient_submit_failure_is_persisted(
    runner: CliRunner,
    empty_sha256_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = TransientSubmitFake()
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner,
        empty_sha256_root,
        staging,
        "obj:transient",
        ("a.txt", b"a"),
    )

    result = runner.invoke(
        app,
        stage_args(
            empty_sha256_root,
            staging,
            "batch",
            "create",
            "content",
            "obj:transient",
        ),
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert fake.calls == 1
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))
    batch = batch_state.load_batch(staging, batch_id)
    assert batch.submissions[0].status == "failed"
    assert batch.submissions[0].last_error == "SimpleProof HTTP 503"
    assert batch.objects[0].status == "prepared"
    assert batch.objects[0].last_error == "transient_sp_error"


def test_batch_retry_no_resubmit_if_id_present(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner, empty_sha256_root, staging, "obj:no-retry", ("a.txt", b"a")
    )
    create = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "create",
        "content",
        "obj:no-retry",
    )
    assert create.exit_code == ExitCode.SUCCESS, create.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))
    assert len(sp_fake._submissions) == 1

    result = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "retry",
        batch_id,
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert len(sp_fake._submissions) == 1
    batch = batch_state.load_batch(staging, batch_id)
    assert batch.submissions[0].status == "submitted"
    assert batch.objects[0].status == "submitted"


def test_batch_retry_bidirectional_record_id_propagation(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner,
        empty_sha256_root,
        staging,
        "obj:bidirectional",
        ("a.txt", b"a"),
        ("b.txt", b"b"),
    )
    create = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "create",
        "content",
        "obj:bidirectional",
    )
    assert create.exit_code == ExitCode.SUCCESS, create.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))
    batch = batch_state.load_batch(staging, batch_id)
    digest_a, digest_b = batch.objects[0].digests
    batch_state.save_batch(
        staging,
        replace(
            batch,
            objects=(replace(batch.objects[0], status="prepared"),),
            submissions=(
                BatchSubmission(
                    chunk_index=0,
                    digests=(digest_a,),
                    status="submitted",
                    record_ids=("batch-rec-a",),
                    submitted_at="2026-05-12T10:01:00Z",
                    attempts=1,
                ),
                BatchSubmission(
                    chunk_index=1,
                    digests=(digest_a, digest_b),
                    status="failed",
                    attempts=1,
                    last_error="SimpleProof HTTP 503",
                ),
            ),
        ),
    )
    staging_dir = StagingStore(staging).staging_path(
        str(empty_sha256_root.resolve()), "obj:bidirectional"
    )
    state = load_ots(staging_dir)
    save_ots(
        staging_dir,
        replace(
            state,
            phase=Phase.DRAFTING,
            submissions=(
                replace(
                    state.submissions[0],
                    status="submitting",
                    record_ids=(None, "object-rec-b"),
                ),
            ),
        ),
    )

    result = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "retry",
        batch_id,
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert len(sp_fake._submissions) == 1
    updated = batch_state.load_batch(staging, batch_id)
    assert updated.objects[0].status == "submitted"
    assert updated.submissions[-1].status == "submitted"
    assert updated.submissions[-1].digests == (digest_a, digest_b)
    assert updated.submissions[-1].record_ids == ("batch-rec-a", "object-rec-b")
    state = load_ots(staging_dir)
    assert state.phase is Phase.CONTENT_PENDING
    assert state.submissions[0].status == "submitted"
    assert state.submissions[0].record_ids == ("batch-rec-a", "object-rec-b")


def test_batch_retry_failed_chunk_partial_repair(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner,
        empty_sha256_root,
        staging,
        "obj:partial-repair",
        ("a.txt", b"a"),
        ("b.txt", b"b"),
        ("c.txt", b"c"),
    )
    create = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "create",
        "content",
        "obj:partial-repair",
    )
    assert create.exit_code == ExitCode.SUCCESS, create.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))
    batch = batch_state.load_batch(staging, batch_id)
    digest_a, digest_b, digest_c = batch.objects[0].digests
    batch_state.save_batch(
        staging,
        replace(
            batch,
            objects=(replace(batch.objects[0], status="prepared"),),
            submissions=(
                BatchSubmission(
                    chunk_index=0,
                    digests=(digest_a, digest_b, digest_c),
                    status="failed",
                    attempts=1,
                    last_error="SimpleProof HTTP 503",
                ),
                BatchSubmission(
                    chunk_index=1,
                    digests=(digest_a, digest_b),
                    status="failed",
                    attempts=1,
                    last_error="SimpleProof HTTP 503",
                ),
            ),
        ),
    )
    staging_dir = StagingStore(staging).staging_path(
        str(empty_sha256_root.resolve()), "obj:partial-repair"
    )
    state = load_ots(staging_dir)
    save_ots(
        staging_dir,
        replace(
            state,
            phase=Phase.DRAFTING,
            submissions=(
                replace(
                    state.submissions[0],
                    status="submitting",
                    record_ids=("object-rec-a", None, None),
                ),
            ),
        ),
    )
    sp_fake._submissions.clear()

    result = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "retry",
        batch_id,
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert len(sp_fake._submissions) == 1
    assert sp_fake._submissions[0].digests == (digest_b, digest_c)
    updated = batch_state.load_batch(staging, batch_id)
    assert updated.submissions[0].status == "failed"
    assert updated.submissions[1].status == "failed"
    repair, retry = updated.submissions[2:]
    assert repair.status == "submitted"
    assert repair.digests == (digest_a,)
    assert repair.record_ids == ("object-rec-a",)
    assert repair.attempts == 0
    assert retry.status == "submitted"
    assert retry.digests == (digest_b, digest_c)
    assert retry.record_ids == sp_fake._submissions[0].record_ids
    assert retry.attempts == 1
    assert updated.objects[0].status == "submitted"
    state = load_ots(staging_dir)
    assert state.submissions[0].status == "submitted"
    assert state.submissions[0].record_ids == (
        "object-rec-a",
        *sp_fake._submissions[0].record_ids,
    )


def test_batch_retry_inventory_propagates_batch_record_id(
    runner: CliRunner,
    storage_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    batch_id = new_batch_id()
    digest = "sha256:" + ("a" * 64)
    root_path = str(storage_root)
    _ots_staging(
        staging,
        root_path,
        "obj:inventory-retry",
        OtsState(
            phase=Phase.CONTENT_UPGRADED,
            category_id=641,
            inventory=StagedInventory(version="v1", digest=digest),
            submissions=(_inv_submission(digest, batch_id, None),),
        ),
    )
    save_batch(
        staging,
        BatchFile(
            batch_id=batch_id,
            target=TimestampTarget.INVENTORY,
            root_path=root_path,
            category_id=641,
            created_at="2026-05-12T10:00:00Z",
            updated_at="2026-05-12T10:00:00Z",
            objects=(
                BatchObject(
                    object_id="obj:inventory-retry",
                    status="prepared",
                    digests=(digest,),
                ),
            ),
            submissions=(
                BatchSubmission(
                    chunk_index=0,
                    digests=(digest,),
                    status="submitted",
                    record_ids=("batch-rec-inv",),
                    submitted_at="2026-05-12T10:01:00Z",
                    attempts=1,
                ),
            ),
        ),
    )

    result = _invoke(
        runner,
        storage_root,
        staging,
        "batch",
        "retry",
        batch_id,
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert sp_fake._submissions == []
    updated = batch_state.load_batch(staging, batch_id)
    assert updated.objects[0].status == "submitted"
    state = load_ots(
        StagingStore(staging).staging_path(root_path, "obj:inventory-retry")
    )
    (sub,) = [s for s in state.submissions if s.target == "inventory"]
    assert sub.record_ids == ("batch-rec-inv",)
    assert sub.status == "submitted"


def test_batch_retry_pending_chunk_posts_after_crash_before_post(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner, empty_sha256_root, staging, "obj:pending-retry", ("a.txt", b"a")
    )
    create = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "create",
        "content",
        "obj:pending-retry",
    )
    assert create.exit_code == ExitCode.SUCCESS, create.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))
    batch = batch_state.load_batch(staging, batch_id)
    digest = batch.objects[0].digests[0]
    batch_state.save_batch(
        staging,
        replace(
            batch,
            objects=(replace(batch.objects[0], status="prepared"),),
            submissions=(
                BatchSubmission(chunk_index=0, digests=(digest,), status="pending"),
            ),
        ),
    )
    staging_dir = StagingStore(staging).staging_path(
        str(empty_sha256_root.resolve()), "obj:pending-retry"
    )
    state = load_ots(staging_dir)
    save_ots(
        staging_dir,
        replace(
            state,
            phase=Phase.DRAFTING,
            submissions=(
                replace(
                    state.submissions[0],
                    status="submitting",
                    record_ids=(None,),
                ),
            ),
        ),
    )
    sp_fake._submissions.clear()

    result = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "retry",
        batch_id,
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert len(sp_fake._submissions) == 1
    assert sp_fake._submissions[0].digests == (digest,)
    updated = batch_state.load_batch(staging, batch_id)
    assert updated.submissions[0].status == "submitted"
    assert updated.submissions[0].record_ids == sp_fake._submissions[0].record_ids
    assert updated.submissions[0].attempts == 1
    assert updated.objects[0].status == "submitted"


def test_batch_retry_revalidates_before_post_and_marks_regressed(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner, empty_sha256_root, staging, "obj:retry-regressed", ("a.txt", b"a")
    )
    create = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "create",
        "content",
        "obj:retry-regressed",
    )
    assert create.exit_code == ExitCode.SUCCESS, create.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))
    batch = batch_state.load_batch(staging, batch_id)
    digest = batch.objects[0].digests[0]
    batch_state.save_batch(
        staging,
        replace(
            batch,
            objects=(replace(batch.objects[0], status="prepared"),),
            submissions=(
                BatchSubmission(
                    chunk_index=0,
                    digests=(digest,),
                    status="failed",
                    attempts=1,
                    last_error="SimpleProof HTTP 503",
                ),
            ),
        ),
    )
    staging_dir = StagingStore(staging).staging_path(
        str(empty_sha256_root.resolve()), "obj:retry-regressed"
    )
    state = load_ots(staging_dir)
    save_ots(staging_dir, replace(state, submissions=()))
    sp_fake._submissions.clear()

    result = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "retry",
        batch_id,
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert sp_fake._submissions == []
    updated = batch_state.load_batch(staging, batch_id)
    assert updated.objects[0].status == "failed"
    assert updated.objects[0].last_error == "regressed"
    assert updated.submissions[0].status == "failed"


def test_batch_retry_object_record_id_wins_for_non_failed_chunk(
    runner: CliRunner,
    storage_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    batch_id = new_batch_id()
    digest = "sha256:" + ("b" * 64)
    root_path = str(storage_root)
    _ots_staging(
        staging,
        root_path,
        "obj:conflict",
        OtsState(
            phase=Phase.DRAFTING,
            category_id=641,
            submissions=(
                Submission(
                    submitted_at="2026-05-12T10:00:00Z",
                    digests=(digest,),
                    status="submitting",
                    target=TimestampTarget.CONTENT,
                    batch_id=batch_id,
                    record_ids=("object-rec",),
                ),
            ),
        ),
    )
    save_batch(
        staging,
        BatchFile(
            batch_id=batch_id,
            target=TimestampTarget.CONTENT,
            root_path=root_path,
            category_id=641,
            created_at="2026-05-12T10:00:00Z",
            updated_at="2026-05-12T10:00:00Z",
            objects=(
                BatchObject(
                    object_id="obj:conflict",
                    status="prepared",
                    digests=(digest,),
                ),
            ),
            submissions=(
                BatchSubmission(
                    chunk_index=0,
                    digests=(digest,),
                    status="submitted",
                    record_ids=("batch-rec",),
                    submitted_at="2026-05-12T10:01:00Z",
                    attempts=1,
                ),
            ),
        ),
    )

    result = _invoke(
        runner,
        storage_root,
        staging,
        "batch",
        "retry",
        batch_id,
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert sp_fake._submissions == []
    updated = batch_state.load_batch(staging, batch_id)
    assert updated.objects[0].status == "submitted"
    assert updated.submissions[0].status == "submitted"
    assert updated.submissions[0].record_ids == ("object-rec",)
    state = load_ots(StagingStore(staging).staging_path(root_path, "obj:conflict"))
    assert state.submissions[0].record_ids == ("object-rec",)


def _status_json_and_human(
    runner: CliRunner, storage_root: Path, staging: Path, batch_id: str
) -> tuple[dict, str]:
    json_result = _invoke_json(
        runner, storage_root, staging, "batch", "status", batch_id
    )
    human_result = _invoke(runner, storage_root, staging, "batch", "status", batch_id)
    assert json_result.exit_code == ExitCode.SUCCESS, json_result.output
    assert human_result.exit_code == ExitCode.SUCCESS, human_result.output
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
            target=TimestampTarget.CONTENT,
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
            target=TimestampTarget.INVENTORY,
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

    result = _invoke_json(runner, storage_root, staging, "batch", "ls")

    assert result.exit_code == ExitCode.SUCCESS, result.output
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
            target=TimestampTarget.CONTENT,
            root_path=str(storage_root),
            category_id=641,
            created_at="2026-05-12T10:00:00Z",
            updated_at="2026-05-12T10:05:00Z",
            objects=(BatchObject(object_id="obj:1", status="upgraded"),),
        ),
    )

    result = _invoke(runner, storage_root, staging, "batch", "ls")

    assert result.exit_code == ExitCode.SUCCESS, result.output
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
                    target=TimestampTarget.CONTENT,
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
            proofs={digest_a: Proof(target=TimestampTarget.CONTENT, verified=True)},
        ),
    )
    save_batch(
        staging,
        BatchFile(
            batch_id=batch_id,
            target=TimestampTarget.CONTENT,
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

    result = _invoke_json(runner, storage_root, staging, "batch", "status", batch_id)

    assert result.exit_code == ExitCode.SUCCESS, result.output
    data = json.loads(result.output)
    assert "event" not in data
    assert {"batch_id", "objects", "counts"}.issubset(data)
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
            "next": f"ocfl stage batch upgrade {batch_id} (later)",
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
            target=TimestampTarget.CONTENT,
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
            target=TimestampTarget.CONTENT,
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
        expected=f"ocfl stage batch submit {batch_id}",
    )
    _assert_next_consistent(
        data,
        output,
        object_id="obj:partial",
        expected=f"ocfl stage batch retry {batch_id}",
    )
    _assert_next_consistent(
        data,
        output,
        object_id="obj:needs-upgrade",
        expected=f"ocfl stage batch upgrade {batch_id} --watch",
    )
    _assert_next_consistent(
        data,
        output,
        object_id="obj:in-progress",
        expected=f"ocfl stage batch upgrade {batch_id} (later)",
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
        expected=(
            f"Batch complete; ocfl stage batch rm {complete_batch_id} to clean up"
        ),
    )


@pytest.mark.parametrize(
    ("reason", "status", "expected_template"),
    [
        ("regressed", "failed", "ocfl stage status obj:reason"),
        (
            "missing_record_ids",
            "failed",
            "ocfl stage batch retry {batch_id}",
        ),
        (
            "lock_contended",
            "skipped",
            "ocfl stage batch submit {batch_id} (after the other process exits)",
        ),
        (
            "in_batch=018f1f7e-7b4a-7c6a-8a2b-4a7f4c2e9d10",
            "skipped",
            "ocfl stage batch status 018f1f7e-7b4a-7c6a-8a2b-4a7f4c2e9d10",
        ),
        (
            "category_mismatch=642",
            "skipped",
            "Start a separate batch for that category, or discard the "
            "staging and stage the object again",
        ),
        ("committed", "skipped", "None"),
        ("no_new_digests", "skipped", "None"),
        ("noop", "skipped", "None"),
        ("content_not_upgraded", "skipped", "ocfl stage upgrade obj:reason --watch"),
        (
            "missing_metadata",
            "skipped",
            "ocfl stage metadata obj:reason ... "
            "(or create the batch with --allow-missing-metadata)",
        ),
        (
            "sp_partial_results",
            "submitted",
            "ocfl stage batch upgrade {batch_id} (later)",
        ),
        ("results_expired", "submitted", "ocfl stage batch upgrade {batch_id}"),
        ("verify_job_failed", "submitted", "ocfl stage batch upgrade {batch_id}"),
        (
            "transient_sp_error",
            "prepared",
            "ocfl stage batch retry {batch_id}",
        ),
        ("transient_sp_error", "submitted", "ocfl stage batch upgrade {batch_id}"),
        ("transient_rpc_error", "submitted", "ocfl stage batch upgrade {batch_id}"),
        (
            "reorg_deeper_than_min_conf",
            "failed",
            "Retry after node catches up or investigate chain state",
        ),
        ("attestation_mismatch", "failed", "Operator inspection required"),
        (
            "batch_file_missing",
            "failed",
            "work per object: ocfl stage status OBJECT",
        ),
        (
            "submit_pending",
            "submitted",
            "ocfl stage batch retry {batch_id}",
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
            target=TimestampTarget.CONTENT,
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
                    target=TimestampTarget.CONTENT,
                    batch_id=batch_id,
                    record_ids=("rec-a", None),
                ),
            ),
            proofs={digest_a: Proof(target=TimestampTarget.CONTENT, verified=True)},
        ),
    )
    save_batch(
        staging,
        BatchFile(
            batch_id=batch_id,
            target=TimestampTarget.CONTENT,
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
        "batch",
        "status",
        batch_id,
        "--detail",
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
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


def test_batch_status_missing_file_exits_1(
    runner: CliRunner, storage_root: Path, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    batch_id = new_batch_id()

    result = _invoke_json(runner, storage_root, staging, "batch", "status", batch_id)

    assert result.exit_code == ExitCode.GENERIC
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "batch_file_missing"
    assert error["exit"] == ExitCode.GENERIC
    assert "ocfl stage status OBJECT" in error["message"]


def test_batch_status_corrupt_existing_file_is_not_missing(
    runner: CliRunner, storage_root: Path, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    batch_id = new_batch_id()
    path = staging / "batches" / f"{batch_id}.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not-json")

    result = _invoke(runner, storage_root, staging, "batch", "status", batch_id)

    assert result.exit_code == ExitCode.GENERIC
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
            target=TimestampTarget.CONTENT,
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

    result = _invoke_json(runner, storage_root, staging, "batch", "status", batch_id)

    assert result.exit_code == ExitCode.SUCCESS, result.output
    rows = {row["object_id"]: row for row in json.loads(result.output)["objects"]}
    assert rows["obj:1"]["next"] == f"ocfl stage batch retry {batch_id}"
    assert rows["obj:2"]["next"] == f"ocfl stage batch upgrade {batch_id}"


def test_batch_content_upgrade_posts_then_ingests_per_digest(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    btc = FakeBitcoinRPC()
    inject_fake_sp(runner, sp_fake)
    inject_fake_btc(runner, btc)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner,
        empty_sha256_root,
        staging,
        "obj:batch-upgrade",
        ("a.txt", b"alpha"),
    )
    submitted = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "create",
        "content",
        "obj:batch-upgrade",
    )
    assert submitted.exit_code == ExitCode.SUCCESS, submitted.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))
    batch = batch_state.load_batch(staging, batch_id)
    digest = batch.objects[0].digests[0]

    first = _invoke_json(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "upgrade",
        batch_id,
    )
    assert first.exit_code == ExitCode.PENDING, first.output
    events = [json.loads(line) for line in first.output.splitlines()]
    assert [event["event"] for event in events] == [
        "batch_resumed",
        "object_submitted",
        "summary",
    ]
    assert events[1]["next"] == f"ocfl stage batch upgrade {batch_id} (later)"
    batch = batch_state.load_batch(staging, batch_id)
    assert len(batch.verify_jobs) == 1
    assert batch.verify_jobs[0].digests == (digest,)
    _assert_z_timestamp(batch.verify_jobs[0].submitted_at)
    assert batch.objects[0].status == "submitted"

    ots_bytes = ots_for(digest)
    sp_fake.advance_to_completed(batch.verify_jobs[0].job_id, {digest: ots_bytes})
    bury(btc)
    register_attested(btc, digest)

    second = _invoke_json(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "upgrade",
        batch_id,
    )
    assert second.exit_code == ExitCode.SUCCESS, second.output
    events = [json.loads(line) for line in second.output.splitlines()]
    assert [event["event"] for event in events] == [
        "batch_resumed",
        "object_upgraded",
        "summary",
    ]
    assert events[1]["object_id"] == "obj:batch-upgrade"
    assert events[-1]["counts"]["upgraded"] == 1
    batch = batch_state.load_batch(staging, batch_id)
    assert batch.objects[0].status == "upgraded"
    assert batch.verify_jobs[0].completed_at is not None
    _assert_z_timestamp(batch.verify_jobs[0].completed_at)
    state = load_ots(
        StagingStore(staging).staging_path(
            str(empty_sha256_root.resolve()), "obj:batch-upgrade"
        )
    )
    assert state.phase is Phase.CONTENT_UPGRADED
    assert state.proofs[digest].verified is True
    assert (
        ots_cache_path(
            StagingStore(staging).staging_path(
                str(empty_sha256_root.resolve()), "obj:batch-upgrade"
            ),
            digest,
        ).read_bytes()
        == ots_bytes
    )


def test_batch_content_upgrade_marks_partial_results_and_retries_missing_digest(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    btc = FakeBitcoinRPC()
    inject_fake_sp(runner, sp_fake)
    inject_fake_btc(runner, btc)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner,
        empty_sha256_root,
        staging,
        "obj:batch-partial-results",
        ("a.txt", b"alpha"),
        ("b.txt", b"bravo"),
    )
    submitted = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "create",
        "content",
        "obj:batch-partial-results",
    )
    assert submitted.exit_code == ExitCode.SUCCESS, submitted.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))

    first = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "upgrade",
        batch_id,
    )
    assert first.exit_code == ExitCode.PENDING, first.output
    batch = batch_state.load_batch(staging, batch_id)
    assert len(batch.verify_jobs) == 1
    assert len(batch.verify_jobs[0].digests) == 2
    returned_digest, missing_digest = batch.verify_jobs[0].digests

    sp_fake.advance_to_completed(
        batch.verify_jobs[0].job_id, {returned_digest: ots_for(returned_digest)}
    )
    bury(btc)
    register_attested(btc, returned_digest)

    partial = _invoke_json(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "upgrade",
        batch_id,
    )
    assert partial.exit_code == ExitCode.PENDING, partial.output
    events = [json.loads(line) for line in partial.output.splitlines()]
    assert events[1]["event"] == "object_submitted"
    assert events[1]["last_error"] == "sp_partial_results"
    assert events[1]["next"] == f"ocfl stage batch upgrade {batch_id} (later)"
    batch = batch_state.load_batch(staging, batch_id)
    assert batch.objects[0].status == "submitted"
    assert batch.objects[0].last_error == "sp_partial_results"
    state = load_ots(
        StagingStore(staging).staging_path(
            str(empty_sha256_root.resolve()), "obj:batch-partial-results"
        )
    )
    assert state.proofs[returned_digest].verified is True
    missing_proof = state.proofs.get(missing_digest)
    assert missing_proof is None or missing_proof.verified is False

    retry = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "upgrade",
        batch_id,
    )
    assert retry.exit_code == ExitCode.PENDING, retry.output
    batch = batch_state.load_batch(staging, batch_id)
    assert [job.digests for job in batch.verify_jobs] == [
        (returned_digest, missing_digest),
        (missing_digest,),
    ]

    sp_fake.advance_to_completed(
        batch.verify_jobs[1].job_id, {missing_digest: ots_for(missing_digest)}
    )
    register_attested(btc, missing_digest)
    completed = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "upgrade",
        batch_id,
    )
    assert completed.exit_code == ExitCode.SUCCESS, completed.output
    batch = batch_state.load_batch(staging, batch_id)
    assert batch.objects[0].status == "upgraded"
    assert batch.objects[0].last_error is None


def test_batch_watch_subscribes_once_after_initial_upgrade_pass(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, sp_fake)
    inject_fake_btc(runner, FakeBitcoinRPC())

    runner_obj(runner)["sleeper"] = RecordingSleeper()
    subscribers = 0

    class _InterruptingStream:
        def wait_for_block(self, timeout_ms: int) -> bytes | None:
            raise KeyboardInterrupt

    def subscriber_factory(*args, **kwargs):
        nonlocal subscribers
        subscribers += 1
        return _InterruptingStream()

    runner_obj(runner)["block_stream_factory"] = subscriber_factory
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner,
        empty_sha256_root,
        staging,
        "obj:batch-watch",
        ("a.txt", b"alpha"),
    )

    result = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "create",
        "content",
        "obj:batch-watch",
        "--watch",
    )

    assert result.exit_code == ExitCode.INTERRUPTED, result.output
    assert "batch submitted:" in result.output
    assert "waiting for Bitcoin blocks" in result.output
    assert subscribers == 1
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))
    batch = batch_state.load_batch(staging, batch_id)
    assert len(batch.verify_jobs) == 1
    assert batch.verify_jobs[0].status == "IN_PROGRESS"


def test_batch_watch_human_output_reports_observed_blocks(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    btc = FakeBitcoinRPC()
    inject_fake_sp(runner, sp_fake)
    inject_fake_btc(runner, btc)

    runner_obj(runner)["sleeper"] = RecordingSleeper()

    class _OneBlockStream:
        seen = False

        def wait_for_block(self, timeout_ms: int) -> bytes | None:
            if not self.seen:
                self.seen = True
                btc.extend(1)
                return bytes.fromhex(btc.getblockhash(btc.tip_height))
            raise KeyboardInterrupt

    runner_obj(runner)["block_stream_factory"] = lambda *a, **kw: _OneBlockStream()
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner,
        empty_sha256_root,
        staging,
        "obj:batch-watch-block",
        ("a.txt", b"alpha"),
    )

    result = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "create",
        "content",
        "obj:batch-watch-block",
        "--watch",
    )

    assert result.exit_code == ExitCode.INTERRUPTED, result.output
    assert f"block 1 {stage_batch.short_block_hash(btc.getblockhash(1))}" in (
        result.output
    )
    assert "submitted=1 upgraded=0 failed=0" in result.output
    assert "verified=0/1 jobs=1 in_progress" in result.output


def test_batch_watch_polls_during_quiet_periods(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    btc = FakeBitcoinRPC()
    inject_fake_sp(runner, sp_fake)
    inject_fake_btc(runner, btc)

    subscribers = 0
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner,
        empty_sha256_root,
        staging,
        "obj:batch-watch-quiet",
        ("a.txt", b"alpha"),
    )

    class _QuietCompletingStream:
        timeouts = 0

        def wait_for_block(self, timeout_ms: int) -> bytes | None:
            assert timeout_ms == 5000
            self.timeouts += 1
            if self.timeouts == 1:
                batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))
                batch = batch_state.load_batch(staging, batch_id)
                digest = batch.objects[0].digests[0]
                sp_fake.advance_to_completed(
                    batch.verify_jobs[0].job_id, {digest: ots_for(digest)}
                )
                bury(btc)
                register_attested(btc, digest)
            if self.timeouts > 3:
                raise KeyboardInterrupt
            return None

    def subscriber_factory(*args, **kwargs):
        nonlocal subscribers
        subscribers += 1
        return _QuietCompletingStream()

    runner_obj(runner)["block_stream_factory"] = subscriber_factory

    result = _invoke_json(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "create",
        "content",
        "obj:batch-watch-quiet",
        "--watch",
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert subscribers == 1
    events = [json.loads(line) for line in result.output.splitlines()]
    assert events[-1]["event"] == "summary"
    assert events[-1]["counts"]["upgraded"] == 1
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))
    batch = batch_state.load_batch(staging, batch_id)
    assert batch.objects[0].status == "upgraded"


def test_batch_watch_empty_submitted_subset_exits_without_subscriber(
    runner: CliRunner,
    empty_sha256_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, TransientSubmitFake())
    inject_fake_btc(runner, FakeBitcoinRPC())

    runner_obj(runner)["block_stream_factory"] = lambda *a, **kw: pytest.fail(
        "watch subscribed with no submitted rows",
    )
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner,
        empty_sha256_root,
        staging,
        "obj:batch-watch-empty",
        ("a.txt", b"alpha"),
    )

    result = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "create",
        "content",
        "obj:batch-watch-empty",
        "--watch",
    )

    assert result.exit_code == ExitCode.GENERIC, result.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))
    batch = batch_state.load_batch(staging, batch_id)
    assert batch.objects[0].status == "prepared"
    assert batch.objects[0].last_error == "transient_sp_error"
    assert batch.verify_jobs == ()


def test_batch_watch_all_skipped_exits_without_subscriber(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, sp_fake)
    inject_fake_btc(runner, FakeBitcoinRPC())

    runner_obj(runner)["block_stream_factory"] = lambda *a, **kw: pytest.fail(
        "watch subscribed with only skipped rows",
    )
    staging = tmp_path / "staging"

    result = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "create",
        "content",
        "obj:not-staged",
        "--watch",
    )

    assert result.exit_code == ExitCode.GENERIC, result.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))
    batch = batch_state.load_batch(staging, batch_id)
    assert batch.objects[0].status == "skipped"


def test_batch_watch_enters_upgrade_for_submitted_subset_after_submit_failure(
    runner: CliRunner,
    empty_sha256_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    sp = SecondSubmitTransientFake()
    inject_fake_sp(runner, sp)
    inject_fake_btc(runner, FakeBitcoinRPC())

    sp.submit_chunk_size = 1
    runner_obj(runner)["sleeper"] = RecordingSleeper()

    class _InterruptingStream:
        def wait_for_block(self, timeout_ms: int) -> bytes | None:
            raise KeyboardInterrupt

    _inject_fake_zmq(runner, _InterruptingStream())
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner,
        empty_sha256_root,
        staging,
        "obj:watch-partial-a",
        ("a.txt", b"alpha"),
    )
    _new_ots_with_files(
        runner,
        empty_sha256_root,
        staging,
        "obj:watch-partial-b",
        ("b.txt", b"bravo"),
    )

    result = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "create",
        "content",
        "obj:watch-partial-a",
        "obj:watch-partial-b",
        "--watch",
    )

    assert result.exit_code == ExitCode.INTERRUPTED, result.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))
    batch = batch_state.load_batch(staging, batch_id)
    assert sorted(row.status for row in batch.objects) == ["prepared", "submitted"]
    assert any(row.last_error == "transient_sp_error" for row in batch.objects)
    assert len(batch.verify_jobs) == 1
    assert len(sp._jobs) == 1


def test_batch_watch_revalidates_cached_proofs_on_height_jump(
    runner: CliRunner,
    storage_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    btc = FakeBitcoinRPC()
    inject_fake_sp(runner, sp_fake)
    inject_fake_btc(runner, btc)

    runner_obj(runner)["sleeper"] = RecordingSleeper()
    root_path = str(storage_root)
    staging = tmp_path / "staging"
    batch_id = new_batch_id()
    verified_digest = "a" * 64
    unverified_digest = "b" * 64
    staging_dir = _ots_staging(
        staging,
        root_path,
        "obj:watch-reverify",
        OtsState(
            phase=Phase.CONTENT_PENDING,
            category_id=641,
            submissions=(
                Submission(
                    submitted_at="2026-05-12T10:00:00Z",
                    digests=(verified_digest, unverified_digest),
                    status="submitted",
                    target=TimestampTarget.CONTENT,
                    batch_id=batch_id,
                    record_ids=("rec-a", "rec-b"),
                ),
            ),
            proofs={
                verified_digest: Proof(
                    target=TimestampTarget.CONTENT,
                    ots_path=f"ots-cache/{verified_digest}.ots",
                    verified=True,
                )
            },
        ),
    )
    ots_cache_path(staging_dir, verified_digest).parent.mkdir(parents=True)
    ots_cache_path(staging_dir, verified_digest).write_bytes(ots_for(UNRELATED_DIGEST))
    save_batch(
        staging,
        BatchFile(
            batch_id=batch_id,
            target=TimestampTarget.CONTENT,
            root_path=root_path,
            category_id=641,
            created_at="2026-05-12T10:00:00Z",
            updated_at="2026-05-12T10:00:00Z",
            objects=(
                BatchObject(
                    object_id="obj:watch-reverify",
                    status="submitted",
                    digests=(verified_digest, unverified_digest),
                ),
            ),
        ),
    )

    class _JumpStream:
        def __init__(self) -> None:
            self.calls = 0

        def wait_for_block(self, timeout_ms: int) -> bytes | None:
            self.calls += 1
            if self.calls == 1:
                return bytes.fromhex(btc.getblockchaininfo().best_block_hash)
            if self.calls == 2:
                btc.extend(_HEIGHT_JUMP)
                return bytes.fromhex(btc.getblockchaininfo().best_block_hash)
            raise KeyboardInterrupt

    _inject_fake_zmq(runner, _JumpStream())

    result = _invoke(
        runner,
        storage_root,
        staging,
        "batch",
        "upgrade",
        batch_id,
        "--watch",
    )

    assert result.exit_code == ExitCode.INTERRUPTED, result.output
    batch = batch_state.load_batch(staging, batch_id)
    assert len(sp_fake._jobs) == 2
    assert any(job.digests == (verified_digest,) for job in batch.verify_jobs)
    state = load_ots(staging_dir)
    assert state.proofs[verified_digest].verified is False


def test_batch_watch_revalidates_mixed_upgraded_content_row(
    runner: CliRunner,
    storage_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    btc = FakeBitcoinRPC()
    inject_fake_sp(runner, sp_fake)
    inject_fake_btc(runner, btc)

    runner_obj(runner)["sleeper"] = RecordingSleeper()
    root_path = str(storage_root)
    staging = tmp_path / "staging"
    batch_id = new_batch_id()
    upgraded_digest = "c" * 64
    waiting_digest = "d" * 64
    upgraded_dir = _ots_staging(
        staging,
        root_path,
        "obj:watch-upgraded",
        OtsState(
            phase=Phase.CONTENT_UPGRADED,
            category_id=641,
            submissions=(
                Submission(
                    submitted_at="2026-05-12T10:00:00Z",
                    digests=(upgraded_digest,),
                    status="submitted",
                    target=TimestampTarget.CONTENT,
                    batch_id=batch_id,
                    record_ids=("rec-upgraded",),
                ),
            ),
            proofs={
                upgraded_digest: Proof(
                    target=TimestampTarget.CONTENT,
                    ots_path=f"ots-cache/{upgraded_digest}.ots",
                    verified=True,
                )
            },
        ),
    )
    waiting_dir = _ots_staging(
        staging,
        root_path,
        "obj:watch-waiting",
        OtsState(
            phase=Phase.CONTENT_PENDING,
            category_id=641,
            submissions=(
                Submission(
                    submitted_at="2026-05-12T10:00:00Z",
                    digests=(waiting_digest,),
                    status="submitted",
                    target=TimestampTarget.CONTENT,
                    batch_id=batch_id,
                    record_ids=("rec-waiting",),
                ),
            ),
        ),
    )
    ots_cache_path(upgraded_dir, upgraded_digest).parent.mkdir(parents=True)
    ots_cache_path(upgraded_dir, upgraded_digest).write_bytes(ots_for(UNRELATED_DIGEST))
    save_batch(
        staging,
        BatchFile(
            batch_id=batch_id,
            target=TimestampTarget.CONTENT,
            root_path=root_path,
            category_id=641,
            created_at="2026-05-12T10:00:00Z",
            updated_at="2026-05-12T10:00:00Z",
            objects=(
                BatchObject(
                    object_id="obj:watch-upgraded",
                    status="upgraded",
                    digests=(upgraded_digest,),
                ),
                BatchObject(
                    object_id="obj:watch-waiting",
                    status="submitted",
                    digests=(waiting_digest,),
                ),
            ),
        ),
    )

    class _JumpStream:
        def __init__(self) -> None:
            self.calls = 0

        def wait_for_block(self, timeout_ms: int) -> bytes | None:
            self.calls += 1
            if self.calls == 1:
                return bytes.fromhex(btc.getblockchaininfo().best_block_hash)
            if self.calls == 2:
                btc.extend(_HEIGHT_JUMP)
                return bytes.fromhex(btc.getblockchaininfo().best_block_hash)
            raise KeyboardInterrupt

    _inject_fake_zmq(runner, _JumpStream())

    result = _invoke(
        runner,
        storage_root,
        staging,
        "batch",
        "upgrade",
        batch_id,
        "--watch",
    )

    assert result.exit_code == ExitCode.INTERRUPTED, result.output
    batch = batch_state.load_batch(staging, batch_id)
    rows = {row.object_id: row for row in batch.objects}
    assert rows["obj:watch-upgraded"].status == "submitted"
    assert rows["obj:watch-waiting"].status == "submitted"
    assert any(job.digests == (waiting_digest,) for job in batch.verify_jobs)
    assert any(job.digests == (upgraded_digest,) for job in batch.verify_jobs)
    upgraded_state = load_ots(upgraded_dir)
    assert upgraded_state.phase is Phase.CONTENT_PENDING
    assert upgraded_state.proofs[upgraded_digest].verified is False
    assert load_ots(waiting_dir).phase is Phase.CONTENT_PENDING


def test_batch_watch_revalidates_mixed_upgraded_inventory_row(
    runner: CliRunner,
    storage_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    btc = FakeBitcoinRPC()
    inject_fake_sp(runner, sp_fake)
    inject_fake_btc(runner, btc)

    runner_obj(runner)["sleeper"] = RecordingSleeper()
    root_path = str(storage_root)
    staging = tmp_path / "staging"
    batch_id = new_batch_id()
    upgraded_digest = "e" * 64
    waiting_digest = "f" * 64
    upgraded_dir = _ots_staging(
        staging,
        root_path,
        "obj:inventory-upgraded",
        OtsState(
            phase=Phase.INVENTORY_UPGRADED,
            category_id=641,
            inventory=StagedInventory(version="v1", digest=upgraded_digest),
            submissions=(_inv_submission(upgraded_digest, batch_id, "rec-upgraded"),),
            proofs={
                upgraded_digest: Proof(
                    target=TimestampTarget.INVENTORY,
                    ots_path=f"ots-cache/{upgraded_digest}.ots",
                    verified=True,
                )
            },
        ),
    )
    waiting_dir = _ots_staging(
        staging,
        root_path,
        "obj:inventory-waiting",
        OtsState(
            phase=Phase.INVENTORY_PENDING,
            category_id=641,
            inventory=StagedInventory(version="v1", digest=waiting_digest),
            submissions=(_inv_submission(waiting_digest, batch_id, "rec-waiting"),),
            proofs={
                waiting_digest: Proof(target=TimestampTarget.INVENTORY, verified=False)
            },
        ),
    )
    ots_cache_path(upgraded_dir, upgraded_digest).parent.mkdir(parents=True)
    ots_cache_path(upgraded_dir, upgraded_digest).write_bytes(ots_for(UNRELATED_DIGEST))
    save_batch(
        staging,
        BatchFile(
            batch_id=batch_id,
            target=TimestampTarget.INVENTORY,
            root_path=root_path,
            category_id=641,
            created_at="2026-05-12T10:00:00Z",
            updated_at="2026-05-12T10:00:00Z",
            objects=(
                BatchObject(
                    object_id="obj:inventory-upgraded",
                    status="upgraded",
                    digests=(upgraded_digest,),
                ),
                BatchObject(
                    object_id="obj:inventory-waiting",
                    status="submitted",
                    digests=(waiting_digest,),
                ),
            ),
        ),
    )

    class _JumpStream:
        def __init__(self) -> None:
            self.calls = 0

        def wait_for_block(self, timeout_ms: int) -> bytes | None:
            self.calls += 1
            if self.calls == 1:
                return bytes.fromhex(btc.getblockchaininfo().best_block_hash)
            if self.calls == 2:
                btc.extend(_HEIGHT_JUMP)
                return bytes.fromhex(btc.getblockchaininfo().best_block_hash)
            raise KeyboardInterrupt

    _inject_fake_zmq(runner, _JumpStream())

    result = _invoke(
        runner,
        storage_root,
        staging,
        "batch",
        "upgrade",
        batch_id,
        "--watch",
    )

    assert result.exit_code == ExitCode.INTERRUPTED, result.output
    batch = batch_state.load_batch(staging, batch_id)
    rows = {row.object_id: row for row in batch.objects}
    assert rows["obj:inventory-upgraded"].status == "submitted"
    assert rows["obj:inventory-waiting"].status == "submitted"
    assert any(job.digests == (waiting_digest,) for job in batch.verify_jobs)
    assert any(job.digests == (upgraded_digest,) for job in batch.verify_jobs)
    upgraded_state = load_ots(upgraded_dir)
    assert upgraded_state.phase is Phase.INVENTORY_PENDING
    assert upgraded_state.inventory_proof is not None
    assert upgraded_state.inventory_proof.verified is False
    assert load_ots(waiting_dir).phase is Phase.INVENTORY_PENDING


def test_batch_watch_content_reorg_revalidation_is_object_scoped(
    runner: CliRunner,
    storage_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    btc = FakeBitcoinRPC()
    inject_fake_sp(runner, sp_fake)
    inject_fake_btc(runner, btc)

    runner_obj(runner)["sleeper"] = RecordingSleeper()

    root_path = str(storage_root)
    staging = tmp_path / "staging"
    batch_id = new_batch_id()
    failed_digest = "1" * 64
    waiting_digest = "2" * 64
    failed_dir = _ots_staging(
        staging,
        root_path,
        "obj:content-reorg",
        OtsState(
            phase=Phase.CONTENT_UPGRADED,
            category_id=641,
            submissions=(
                Submission(
                    submitted_at="2026-05-12T10:00:00Z",
                    digests=(failed_digest,),
                    status="submitted",
                    target=TimestampTarget.CONTENT,
                    batch_id=batch_id,
                    record_ids=("rec-failed",),
                ),
            ),
            proofs={
                failed_digest: Proof(
                    target=TimestampTarget.CONTENT,
                    ots_path=f"ots-cache/{failed_digest}.ots",
                    verified=True,
                )
            },
        ),
    )
    _ots_staging(
        staging,
        root_path,
        "obj:content-waiting",
        OtsState(
            phase=Phase.CONTENT_PENDING,
            category_id=641,
            submissions=(
                Submission(
                    submitted_at="2026-05-12T10:00:00Z",
                    digests=(waiting_digest,),
                    status="submitted",
                    target=TimestampTarget.CONTENT,
                    batch_id=batch_id,
                    record_ids=("rec-waiting",),
                ),
            ),
        ),
    )
    ots_cache_path(failed_dir, failed_digest).parent.mkdir(parents=True)
    ots_cache_path(failed_dir, failed_digest).write_bytes(ots_for(failed_digest))
    end_chain_below(btc, _HEIGHT_JUMP + MIN_CONFS + 1)
    save_batch(
        staging,
        BatchFile(
            batch_id=batch_id,
            target=TimestampTarget.CONTENT,
            root_path=root_path,
            category_id=641,
            created_at="2026-05-12T10:00:00Z",
            updated_at="2026-05-12T10:00:00Z",
            objects=(
                BatchObject(
                    object_id="obj:content-reorg",
                    status="upgraded",
                    digests=(failed_digest,),
                ),
                BatchObject(
                    object_id="obj:content-waiting",
                    status="submitted",
                    digests=(waiting_digest,),
                ),
            ),
        ),
    )

    class _JumpStream:
        calls = 0

        def wait_for_block(self, timeout_ms: int) -> bytes | None:
            self.calls += 1
            if self.calls == 1:
                return bytes.fromhex(btc.getblockchaininfo().best_block_hash)
            if self.calls == 2:
                btc.extend(_HEIGHT_JUMP)
                return bytes.fromhex(btc.getblockchaininfo().best_block_hash)
            raise KeyboardInterrupt

    _inject_fake_zmq(runner, _JumpStream())

    result = _invoke(
        runner,
        storage_root,
        staging,
        "batch",
        "upgrade",
        batch_id,
        "--watch",
    )

    assert result.exit_code == ExitCode.INTERRUPTED, result.output
    assert "halt:" not in result.output
    batch = batch_state.load_batch(staging, batch_id)
    rows = {row.object_id: row for row in batch.objects}
    assert rows["obj:content-reorg"].status == "failed"
    assert rows["obj:content-reorg"].last_error == "reorg_deeper_than_min_conf"
    assert rows["obj:content-waiting"].status == "submitted"
    assert all(failed_digest not in job.digests for job in batch.verify_jobs)
    assert any(job.digests == (waiting_digest,) for job in batch.verify_jobs)
    state = load_ots(failed_dir)
    assert state.phase is Phase.CONTENT_PENDING
    assert state.proofs[failed_digest].verified is False


def test_batch_watch_inventory_reorg_revalidation_is_object_scoped(
    runner: CliRunner,
    storage_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    btc = FakeBitcoinRPC()
    inject_fake_sp(runner, sp_fake)
    inject_fake_btc(runner, btc)

    runner_obj(runner)["sleeper"] = RecordingSleeper()

    root_path = str(storage_root)
    staging = tmp_path / "staging"
    batch_id = new_batch_id()
    failed_digest = "3" * 64
    waiting_digest = "4" * 64
    failed_dir = _ots_staging(
        staging,
        root_path,
        "obj:inventory-reorg",
        OtsState(
            phase=Phase.INVENTORY_UPGRADED,
            category_id=641,
            inventory=StagedInventory(version="v1", digest=failed_digest),
            submissions=(_inv_submission(failed_digest, batch_id, "rec-failed"),),
            proofs={
                failed_digest: Proof(
                    target=TimestampTarget.INVENTORY,
                    ots_path=f"ots-cache/{failed_digest}.ots",
                    verified=True,
                )
            },
        ),
    )
    _ots_staging(
        staging,
        root_path,
        "obj:inventory-reorg-waiting",
        OtsState(
            phase=Phase.INVENTORY_PENDING,
            category_id=641,
            inventory=StagedInventory(version="v1", digest=waiting_digest),
            submissions=(_inv_submission(waiting_digest, batch_id, "rec-waiting"),),
            proofs={
                waiting_digest: Proof(target=TimestampTarget.INVENTORY, verified=False)
            },
        ),
    )
    ots_cache_path(failed_dir, failed_digest).parent.mkdir(parents=True)
    ots_cache_path(failed_dir, failed_digest).write_bytes(ots_for(failed_digest))
    end_chain_below(btc, _HEIGHT_JUMP + MIN_CONFS + 1)
    save_batch(
        staging,
        BatchFile(
            batch_id=batch_id,
            target=TimestampTarget.INVENTORY,
            root_path=root_path,
            category_id=641,
            created_at="2026-05-12T10:00:00Z",
            updated_at="2026-05-12T10:00:00Z",
            objects=(
                BatchObject(
                    object_id="obj:inventory-reorg",
                    status="upgraded",
                    digests=(failed_digest,),
                ),
                BatchObject(
                    object_id="obj:inventory-reorg-waiting",
                    status="submitted",
                    digests=(waiting_digest,),
                ),
            ),
        ),
    )

    class _JumpStream:
        calls = 0

        def wait_for_block(self, timeout_ms: int) -> bytes | None:
            self.calls += 1
            if self.calls == 1:
                return bytes.fromhex(btc.getblockchaininfo().best_block_hash)
            if self.calls == 2:
                btc.extend(_HEIGHT_JUMP)
                return bytes.fromhex(btc.getblockchaininfo().best_block_hash)
            raise KeyboardInterrupt

    _inject_fake_zmq(runner, _JumpStream())

    result = _invoke(
        runner,
        storage_root,
        staging,
        "batch",
        "upgrade",
        batch_id,
        "--watch",
    )

    assert result.exit_code == ExitCode.INTERRUPTED, result.output
    assert "halt:" not in result.output
    batch = batch_state.load_batch(staging, batch_id)
    rows = {row.object_id: row for row in batch.objects}
    assert rows["obj:inventory-reorg"].status == "failed"
    assert rows["obj:inventory-reorg"].last_error == "reorg_deeper_than_min_conf"
    assert rows["obj:inventory-reorg-waiting"].status == "submitted"
    assert all(failed_digest not in job.digests for job in batch.verify_jobs)
    assert any(job.digests == (waiting_digest,) for job in batch.verify_jobs)
    state = load_ots(failed_dir)
    assert state.phase is Phase.INVENTORY_PENDING
    assert state.inventory_proof is not None
    assert state.inventory_proof.verified is False


def test_batch_watch_node_halt_exits_operational_error(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, sp_fake)
    inject_fake_btc(runner, TransientBitcoinRPC())

    runner_obj(runner)["sleeper"] = RecordingSleeper()
    stream = InMemoryHashBlockStream()
    stream.push(b"\x00" * 32)
    _inject_fake_zmq(runner, stream)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner,
        empty_sha256_root,
        staging,
        "obj:batch-watch-halt",
        ("a.txt", b"alpha"),
    )

    result = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "create",
        "content",
        "obj:batch-watch-halt",
        "--watch",
    )

    assert result.exit_code == ExitCode.GENERIC, result.output
    assert "halt: Bitcoin RPC timeout" in result.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))
    batch = batch_state.load_batch(staging, batch_id)
    assert batch.objects[0].status == "submitted"
    assert len(batch.verify_jobs) == 1


def test_batch_upgrade_failed_verify_job_is_retryable(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, sp_fake)
    inject_fake_btc(runner, FakeBitcoinRPC())
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner,
        empty_sha256_root,
        staging,
        "obj:batch-failed-job",
        ("a.txt", b"alpha"),
    )
    submitted = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "create",
        "content",
        "obj:batch-failed-job",
    )
    assert submitted.exit_code == ExitCode.SUCCESS, submitted.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))

    first = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "upgrade",
        batch_id,
    )
    assert first.exit_code == ExitCode.PENDING, first.output
    failed_job_id = batch_state.load_batch(staging, batch_id).verify_jobs[0].job_id
    sp_fake.fail_job(failed_job_id)

    failed = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "upgrade",
        batch_id,
    )
    assert failed.exit_code == ExitCode.PENDING, failed.output
    batch = batch_state.load_batch(staging, batch_id)
    assert batch.verify_jobs[0].status == "FAILED"
    assert batch.objects[0].status == "submitted"
    assert batch.objects[0].last_error == "verify_job_failed"

    retried = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "upgrade",
        batch_id,
    )
    assert retried.exit_code == ExitCode.PENDING, retried.output
    batch = batch_state.load_batch(staging, batch_id)
    assert [job.status for job in batch.verify_jobs] == ["FAILED", "IN_PROGRESS"]
    assert len(sp_fake._jobs) == 2


def test_batch_upgrade_poll_transient_marks_retryable(
    runner: CliRunner,
    empty_sha256_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    sp = TransientPollFake()
    inject_fake_sp(runner, sp)
    inject_fake_btc(runner, FakeBitcoinRPC())
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner,
        empty_sha256_root,
        staging,
        "obj:batch-poll-fails",
        ("a.txt", b"alpha"),
    )
    submitted = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "create",
        "content",
        "obj:batch-poll-fails",
    )
    assert submitted.exit_code == ExitCode.SUCCESS, submitted.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))

    upgraded = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "upgrade",
        batch_id,
    )

    assert upgraded.exit_code == ExitCode.PENDING, upgraded.output
    batch = batch_state.load_batch(staging, batch_id)
    assert batch.verify_jobs[0].status == "FAILED"
    assert batch.verify_jobs[0].last_error == "SimpleProof HTTP 503"
    assert batch.objects[0].status == "submitted"
    assert batch.objects[0].last_error == "transient_sp_error"


def test_batch_upgrade_download_mismatch_marks_terminal(
    runner: CliRunner,
    empty_sha256_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    sp = MismatchDownloadFake()
    btc = FakeBitcoinRPC()
    inject_fake_sp(runner, sp)
    inject_fake_btc(runner, btc)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner,
        empty_sha256_root,
        staging,
        "obj:batch-mismatch",
        ("a.txt", b"alpha"),
    )
    submitted = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "create",
        "content",
        "obj:batch-mismatch",
    )
    assert submitted.exit_code == ExitCode.SUCCESS, submitted.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))

    first = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "upgrade",
        batch_id,
    )
    assert first.exit_code == ExitCode.PENDING, first.output
    batch = batch_state.load_batch(staging, batch_id)
    sp.advance_to_completed(batch.verify_jobs[0].job_id, {})

    mismatched = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "upgrade",
        batch_id,
    )

    assert mismatched.exit_code == ExitCode.SUCCESS, mismatched.output
    batch = batch_state.load_batch(staging, batch_id)
    assert batch.verify_jobs[0].status == "COMPLETED"
    assert batch.verify_jobs[0].last_error == "attestation_mismatch"
    assert batch.objects[0].status == "failed"
    assert batch.objects[0].last_error == "attestation_mismatch"


def test_batch_upgrade_bitcoin_transient_marks_retryable(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, sp_fake)
    inject_fake_btc(runner, TransientBitcoinRPC())
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner,
        empty_sha256_root,
        staging,
        "obj:batch-rpc-fails",
        ("a.txt", b"alpha"),
    )
    submitted = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "create",
        "content",
        "obj:batch-rpc-fails",
    )
    assert submitted.exit_code == ExitCode.SUCCESS, submitted.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))

    first = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "upgrade",
        batch_id,
    )
    assert first.exit_code == ExitCode.PENDING, first.output
    batch = batch_state.load_batch(staging, batch_id)
    digest = batch.objects[0].digests[0]
    sp_fake.advance_to_completed(batch.verify_jobs[0].job_id, {digest: ots_for(digest)})

    failed = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "upgrade",
        batch_id,
    )

    assert failed.exit_code == ExitCode.PENDING, failed.output
    batch = batch_state.load_batch(staging, batch_id)
    assert batch.verify_jobs[0].status == "COMPLETED"
    assert batch.objects[0].status == "submitted"
    assert batch.objects[0].last_error == "transient_rpc_error"


def test_batch_upgrade_expired_poll_gets_fresh_job(
    runner: CliRunner,
    empty_sha256_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    sp = ExpiredPollFake()
    inject_fake_sp(runner, sp)
    inject_fake_btc(runner, FakeBitcoinRPC())
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner,
        empty_sha256_root,
        staging,
        "obj:batch-expired",
        ("a.txt", b"alpha"),
    )
    submitted = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "create",
        "content",
        "obj:batch-expired",
    )
    assert submitted.exit_code == ExitCode.SUCCESS, submitted.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))

    first = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "upgrade",
        batch_id,
    )
    assert first.exit_code == ExitCode.PENDING, first.output
    assert batch_state.load_batch(staging, batch_id).verify_jobs[0].status == "EXPIRED"

    second = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "upgrade",
        batch_id,
    )

    assert second.exit_code == ExitCode.PENDING, second.output
    batch = batch_state.load_batch(staging, batch_id)
    assert [job.status for job in batch.verify_jobs] == ["EXPIRED", "EXPIRED"]
    assert len(sp._jobs) == 2


def test_batch_inventory_upgrade_rejects_stale_plan_digest(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    btc = FakeBitcoinRPC()
    inject_fake_sp(runner, sp_fake)
    inject_fake_btc(runner, btc)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner,
        empty_sha256_root,
        staging,
        "obj:inv-stale",
        ("a.txt", b"alpha"),
    )
    store = StagingStore(staging)
    root_path = str(empty_sha256_root.resolve())
    staging_dir = store.staging_path(root_path, "obj:inv-stale")
    digest = hashlib.sha256(b"alpha").hexdigest()
    state = load_ots(staging_dir)
    save_ots(
        staging_dir,
        replace(
            state,
            phase=Phase.CONTENT_UPGRADED,
            proofs={digest: Proof(target=TimestampTarget.CONTENT, verified=True)},
        ),
    )

    submitted = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "create",
        "inventory",
        "obj:inv-stale",
        "--allow-missing-metadata",
    )
    assert submitted.exit_code == ExitCode.SUCCESS, submitted.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))
    batch = batch_state.load_batch(staging, batch_id)
    inv_digest = batch.objects[0].digests[0]

    store.save_metadata(
        root_path,
        "obj:inv-stale",
        StagedMetadata(
            message="changed after submit",
            user=StagedUser(name="Alice", address="mailto:alice@example.org"),
        ),
    )
    first = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "upgrade",
        batch_id,
    )
    assert first.exit_code == ExitCode.PENDING, first.output
    batch = batch_state.load_batch(staging, batch_id)
    ots_bytes = ots_for(inv_digest)
    sp_fake.advance_to_completed(batch.verify_jobs[0].job_id, {inv_digest: ots_bytes})
    bury(btc)
    register_attested(btc, inv_digest)

    upgraded = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "upgrade",
        batch_id,
    )

    assert upgraded.exit_code == ExitCode.SUCCESS, upgraded.output
    batch = batch_state.load_batch(staging, batch_id)
    assert batch.objects[0].status == "failed"
    assert batch.objects[0].last_error == "regressed"
    state = load_ots(staging_dir)
    assert state.inventory_proof is not None
    assert state.inventory_proof.verified is False


def test_batch_rm_refuses_non_terminal_without_force(
    runner: CliRunner, storage_root: Path, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    batch_id = new_batch_id()
    save_batch(
        staging,
        BatchFile(
            batch_id=batch_id,
            target=TimestampTarget.CONTENT,
            root_path=str(storage_root),
            category_id=641,
            created_at="2026-05-12T10:00:00Z",
            updated_at="2026-05-12T10:05:00Z",
            objects=(BatchObject(object_id="obj:1", status="prepared"),),
        ),
    )

    result = _invoke(runner, storage_root, staging, "batch", "rm", batch_id)

    assert result.exit_code == ExitCode.GENERIC
    assert batch_file_path(staging, batch_id).is_file()

    forced = _invoke(runner, storage_root, staging, "batch", "rm", batch_id, "--force")

    assert forced.exit_code == ExitCode.SUCCESS, forced.output
    assert not batch_file_path(staging, batch_id).exists()
    assert not list((staging / "batches").glob(f"{batch_id}*")), (
        "ADR-0005: rm removes the lock file too"
    )


def test_batch_rm_waits_for_a_running_driver(
    runner: CliRunner, storage_root: Path, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    batch_id = new_batch_id()
    save_batch(
        staging,
        BatchFile(
            batch_id=batch_id,
            target=TimestampTarget.CONTENT,
            root_path=str(storage_root),
            category_id=641,
            created_at="2026-05-12T10:00:00Z",
            updated_at="2026-05-12T10:05:00Z",
        ),
    )
    runner_obj(runner)["lock_timeout"] = 0.01

    with held_flock(batch_state.batch_lock_path(staging, batch_id)):
        result = _invoke(runner, storage_root, staging, "batch", "rm", batch_id)

    assert result.exit_code == ExitCode.LOCK_TIMEOUT, result.output
    assert batch_file_path(staging, batch_id).is_file()


def _empty_batch(staging: Path, storage_root: Path, batch_id: str, **kw) -> None:
    save_batch(
        staging,
        BatchFile(
            batch_id=batch_id,
            target=kw.pop("target", TimestampTarget.CONTENT),
            root_path=str(storage_root.resolve()),
            category_id=641,
            created_at="2026-05-12T10:00:00Z",
            updated_at="2026-05-12T10:05:00Z",
            **kw,
        ),
    )


def test_batch_ids_resolve_from_a_unique_prefix(
    runner: CliRunner, storage_root: Path, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    first = "0190a0a0-0000-7000-8000-000000000001"
    second = "0190a0a0-0000-7000-8000-000000000002"
    other = "0190b0b0-0000-7000-8000-000000000003"
    for batch_id in (first, second, other):
        _empty_batch(staging, storage_root, batch_id)

    result = _invoke_json(runner, storage_root, staging, "batch", "status", "0190b")
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert json.loads(result.output)["batch_id"] == other

    ambiguous = _invoke(runner, storage_root, staging, "batch", "status", "0190a0a0")
    assert ambiguous.exit_code == ExitCode.USAGE, ambiguous.output
    assert first in ambiguous.output and second in ambiguous.output

    missing = _invoke(runner, storage_root, staging, "batch", "status", "ffff")
    assert missing.exit_code == ExitCode.GENERIC, missing.output

    removed = _invoke(runner, storage_root, staging, "batch", "rm", "0190b")
    assert removed.exit_code == ExitCode.SUCCESS, removed.output
    assert f"removed batch {other}" in removed.output


def test_unstaged_objects_are_named_and_fail_the_run(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch)
    inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(runner, empty_sha256_root, staging, "obj:ots", ("a.txt", b"a"))
    plain = _invoke(runner, empty_sha256_root, staging, "new", "obj:plain")
    assert plain.exit_code == ExitCode.SUCCESS, plain.output

    result = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "batch",
        "create",
        "content",
        "obj:ots",
        "obj:plain",
        "obj:typo",
    )

    assert result.exit_code == ExitCode.GENERIC, result.output
    assert "obj:plain, obj:typo" in result.output
    (batch_id,) = (p.stem for p in (staging / "batches").glob("*.json"))
    rows = {
        row.object_id: row for row in batch_state.load_batch(staging, batch_id).objects
    }
    assert rows["obj:ots"].status == "submitted"
    assert rows["obj:plain"].skipped_reason == "not_ots"
    assert rows["obj:typo"].skipped_reason == "not_staged"
    status = _invoke_json(
        runner, empty_sha256_root, staging, "batch", "status", batch_id
    )
    nexts = {r["object_id"]: r["next"] for r in json.loads(status.output)["objects"]}
    assert nexts["obj:typo"] == "ocfl stage new obj:typo --ots"
    assert nexts["obj:plain"].startswith("ocfl stage discard obj:plain")


def test_inventory_batch_from_a_content_batch_takes_its_upgraded_rows(
    runner: CliRunner, storage_root: Path, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    content_id = new_batch_id()
    _empty_batch(
        staging,
        storage_root,
        content_id,
        objects=(
            BatchObject(object_id="obj:done", status="upgraded"),
            BatchObject(object_id="obj:waiting", status="submitted"),
        ),
    )
    assert batch_report.upgraded_objects(staging, content_id[:12]) == ("obj:done",)

    inventory_id = new_batch_id()
    _empty_batch(staging, storage_root, inventory_id, target=TimestampTarget.INVENTORY)
    with pytest.raises(UsageError, match="--from takes a content batch"):
        batch_report.upgraded_objects(staging, inventory_id)

    result = _invoke(
        runner,
        storage_root,
        staging,
        "batch",
        "create",
        "content",
        "--from",
        content_id,
    )
    assert result.exit_code == ExitCode.USAGE, result.output
