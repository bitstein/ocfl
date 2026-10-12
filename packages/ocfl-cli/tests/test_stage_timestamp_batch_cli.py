"""CLI tests for local-only batch timestamping commands."""

import hashlib
import json
import multiprocessing
import queue
import time
from pathlib import Path
from typing import cast, get_args, get_origin, get_type_hints, is_typeddict

import pytest
from fakes import FakeBitcoinRPC, FakeSimpleProof, InMemoryHashBlockStream
from msgspec.structs import replace
from runner_helpers import runner_obj
from typer.testing import CliRunner

from ocfl_cli.errors import (
    AttestationMismatchError,
    ReorgDetectedError,
    SimpleProofTransient,
)
from ocfl_cli.main import app
from ocfl_cli.ots.simpleproof import (
    JobStatus,
    SimpleProofDownloadResult,
    SubmitResult,
)
from ocfl_cli.stage import batch as batch_state
from ocfl_cli.stage import commands as stage_commands
from ocfl_cli.stage import service
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
from ocfl_cli.stage.schemas import StagedMetadata, StagedUser
from ocfl_cli.stage.state import (
    InventoryProof,
    OtsState,
    Submission,
    VerifiedProof,
    VerifyJob,
    load_ots,
    ots_cache_path,
    save_ots,
)
from ocfl_cli.stage.store import StagingStore
from ocfl_lib import (
    BitcoinRpcTransientError,
    LocalFS,
    Spec,
    StorageRoot,
    get_layout,
)
from tests.helpers.ots import build_attestation

_SP_OK = """\
[simpleproof]
base_url = "https://stage.example"
proof_category_id = 641
"""

_BTC_OK = """\
[bitcoin]
rpc_url = "http://127.0.0.1:8332"
zmq_endpoint = "tcp://127.0.0.1:28332"
"""


def test_batch_output_helpers_have_typed_contracts() -> None:
    status_hints = get_type_hints(stage_commands._batch_status_dict)
    object_event_hints = get_type_hints(stage_commands._batch_object_event)
    digest_rows_hints = get_type_hints(stage_commands._verbose_digest_rows)
    status_contract = get_type_hints(status_hints["return"])

    assert is_typeddict(status_hints["return"])
    assert is_typeddict(object_event_hints["return"])
    assert get_origin(digest_rows_hints["return"]) is list
    assert is_typeddict(get_args(digest_rows_hints["return"])[0])
    assert is_typeddict(get_args(status_contract["submissions"])[0])
    assert is_typeddict(get_args(status_contract["verify_jobs"])[0])


def test_fixed_batch_reasons_are_canonicalized_through_helper() -> None:
    assert set(stage_commands._FIXED_BATCH_REASONS) == set(
        get_args(stage_commands.FixedBatchReason.__value__)
    )
    assert "sp_partial_results" in stage_commands._FIXED_BATCH_REASONS
    assert [
        stage_commands._fixed_batch_reason(reason)
        for reason in stage_commands._FIXED_BATCH_REASONS
    ] == list(stage_commands._FIXED_BATCH_REASONS)


@pytest.mark.parametrize("status", ["PENDING", "PROCESSING"])
def test_batch_verify_active_statuses_include_simpleproof_v2_states(
    status: batch_state.BatchVerifyJobStatus,
) -> None:
    digest = "a" * 64
    batch = BatchFile(
        batch_id="batch-v2-statuses",
        target="content",
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

    assert stage_commands._verify_job_covers(batch, batch.objects[0])
    assert stage_commands._covered_verify_digests(batch) == {digest}


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


def _write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "ocfl-config" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(_SP_OK + _BTC_OK)
    monkeypatch.setenv("OCFL_CONFIG", str(cfg))
    return cfg


def _inject_fake_sp(runner: CliRunner, fake: FakeSimpleProof) -> None:
    runner_obj(runner)["sp_client_factory"] = lambda cfg: fake


def _inject_fake_btc(runner: CliRunner, fake: FakeBitcoinRPC) -> None:
    runner_obj(runner)["btc_rpc_factory"] = lambda cfg: fake


def _inject_fake_zmq(monkeypatch: pytest.MonkeyPatch, stream) -> None:
    from ocfl_cli.stage import watch as stage_watch

    monkeypatch.setattr(
        stage_watch, "_get_zmq_subscriber", lambda *a, **kw: stream, raising=True
    )


def _stage_args(root: Path, staging: Path, *args: str) -> list[str]:
    return ["--root", str(root), "stage", "--staging-root", str(staging), *args]


DIGEST_BLOCK_HEIGHT = 800_000
MIN_CONFS = 3


def _ots_for(digest_hex: str, *, block_height: int = DIGEST_BLOCK_HEIGHT) -> bytes:
    return build_attestation(bytes.fromhex(digest_hex), block_height=block_height)


def _grow_chain_to(rpc: FakeBitcoinRPC, height: int) -> None:
    needed = height - rpc.tip_height
    if needed > 0:
        rpc.extend(needed)


def _register_attested(
    rpc: FakeBitcoinRPC, digest_hex: str, height: int = DIGEST_BLOCK_HEIGHT
) -> None:
    rpc.register_merkle_root(height, bytes.fromhex(digest_hex)[::-1].hex())


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
    result = runner.invoke(app, _stage_args(Path(root), Path(staging), *args), obj=obj)
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


@pytest.fixture
def empty_sha256_root(tmp_path: Path) -> Path:
    location = tmp_path / "root"
    location.mkdir()
    fs = LocalFS(location)
    layout = get_layout("0002-flat-direct-storage-layout")
    StorageRoot.init(fs, fs, spec=Spec.V1_1, layout=layout)
    return location


def _new_ots_with_files(
    runner: CliRunner,
    root: Path,
    staging: Path,
    object_id: str,
    *files: tuple[str, bytes],
) -> None:
    result = runner.invoke(app, _stage_args(root, staging, "new", object_id, "--ots"))
    assert result.exit_code == 0, result.output
    for name, content in files:
        src = staging.parent / f"{object_id.replace(':', '_')}-{name}"
        src.write_bytes(content)
        result = runner.invoke(
            app, _stage_args(root, staging, "add", object_id, str(src))
        )
        assert result.exit_code == 0, result.output


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
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, sp_fake)
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
        _stage_args(
            empty_sha256_root,
            staging,
            "timestamp",
            "batch",
            "content",
            "--object",
            "obj:one",
            "--object",
            "obj:two",
        ),
    )

    assert result.exit_code == 0, result.output
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
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, sp_fake)
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
            "timestamp",
            "batch",
            "content",
            "--object",
            "obj:json",
        ],
    )

    assert result.exit_code == 0, result.output
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
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(runner, empty_sha256_root, staging, "obj:json", ("a.txt", b"a"))

    result = _invoke_json(
        runner,
        empty_sha256_root,
        staging,
        "timestamp",
        "batch",
        "content",
        "--object",
        "obj:json",
    )

    assert result.exit_code == 0, result.output
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
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner, empty_sha256_root, staging, "obj:resume", ("a.txt", b"a")
    )

    create = runner.invoke(
        app,
        _stage_args(
            empty_sha256_root,
            staging,
            "timestamp",
            "batch",
            "content",
            "--object",
            "obj:resume",
        ),
    )
    assert create.exit_code == 0, create.output
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
        _stage_args(
            empty_sha256_root,
            staging,
            "timestamp",
            "batch",
            "content",
            "--batch",
            batch_id,
        ),
    )

    assert resume.exit_code == 0, resume.output
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
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, sp_fake)
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
            content_proofs={digest: VerifiedProof(verified=True)},
        ),
    )

    result = runner.invoke(
        app,
        _stage_args(
            empty_sha256_root,
            staging,
            "timestamp",
            "batch",
            "inventory",
            "--object",
            "obj:inv",
            "--allow-missing-metadata",
        ),
    )

    assert result.exit_code == 0, result.output
    assert len(sp_fake._submissions) == 1
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))
    batch = batch_state.load_batch(staging, batch_id)
    assert batch.target == "inventory"
    assert batch.objects[0].status == "submitted"
    assert batch.submissions[0].record_ids == ("rec-1",)
    updated = load_ots(staging_dir)
    assert updated.phase is Phase.INVENTORY_PENDING
    assert updated.inventory_proof is not None
    assert batch.objects[0].digests == (updated.inventory_proof.digest,)
    assert batch.submissions[0].digests == (updated.inventory_proof.digest,)
    assert sp_fake._submissions[0].digests == (updated.inventory_proof.digest,)
    assert updated.inventory_proof.batch_id == batch_id
    assert updated.inventory_proof.record_id == "rec-1"


def test_batch_inventory_same_object_skips_until_content_upgraded(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner, empty_sha256_root, staging, "obj:overlap", ("a.txt", b"a")
    )
    content = runner.invoke(
        app,
        _stage_args(
            empty_sha256_root,
            staging,
            "timestamp",
            "batch",
            "content",
            "--object",
            "obj:overlap",
        ),
    )
    assert content.exit_code == 0, content.output

    inventory = runner.invoke(
        app,
        _stage_args(
            empty_sha256_root,
            staging,
            "timestamp",
            "batch",
            "inventory",
            "--object",
            "obj:overlap",
            "--allow-missing-metadata",
        ),
    )

    assert inventory.exit_code == 0, inventory.output
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
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner, empty_sha256_root, staging, "obj:after-content", ("a.txt", b"a")
    )
    content = runner.invoke(
        app,
        _stage_args(
            empty_sha256_root,
            staging,
            "timestamp",
            "batch",
            "content",
            "--object",
            "obj:after-content",
        ),
    )
    assert content.exit_code == 0, content.output
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
            content_proofs={digest: VerifiedProof(verified=True)},
        ),
    )

    inventory = runner.invoke(
        app,
        _stage_args(
            empty_sha256_root,
            staging,
            "timestamp",
            "batch",
            "inventory",
            "--object",
            "obj:after-content",
            "--allow-missing-metadata",
        ),
    )

    assert inventory.exit_code == 0, inventory.output
    batches = [
        batch_state.load_batch(staging, p.stem)
        for p in (staging / "batches").glob("*.json")
    ]
    inventory_batch = next(batch for batch in batches if batch.target == "inventory")
    assert inventory_batch.objects[0].status == "submitted"
    updated = load_ots(staging_dir)
    assert updated.inventory_proof is not None
    assert updated.inventory_proof.batch_id == inventory_batch.batch_id
    assert updated.inventory_proof.record_id is not None


def test_batch_content_shape_mismatch_fails_chunk_closed(
    runner: CliRunner,
    empty_sha256_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = MismatchedRecordIdsFake()
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, fake)
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
        _stage_args(
            empty_sha256_root,
            staging,
            "timestamp",
            "batch",
            "content",
            "--object",
            "obj:mismatch",
        ),
    )

    assert result.exit_code == 0, result.output
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
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner, empty_sha256_root, staging, "obj:failed", ("a.txt", b"a")
    )
    create = runner.invoke(
        app,
        _stage_args(
            empty_sha256_root,
            staging,
            "timestamp",
            "batch",
            "content",
            "--object",
            "obj:failed",
        ),
    )
    assert create.exit_code == 0, create.output
    assert len(fake._submissions) == 1
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))

    resume = runner.invoke(
        app,
        _stage_args(
            empty_sha256_root,
            staging,
            "timestamp",
            "batch",
            "content",
            "--batch",
            batch_id,
        ),
    )

    assert resume.exit_code == 0, resume.output
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
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner, empty_sha256_root, staging, "obj:lease", ("a.txt", b"a")
    )
    original = batch_state.acquire_driver_lease
    observed_clean = []

    def checked_acquire(*args, **kwargs):
        observed_clean.append(not list((staging / "batches").glob("*.json")))
        return original(*args, **kwargs)

    monkeypatch.setattr(batch_state, "acquire_driver_lease", checked_acquire)

    result = runner.invoke(
        app,
        _stage_args(
            empty_sha256_root,
            staging,
            "timestamp",
            "batch",
            "content",
            "--object",
            "obj:lease",
        ),
    )

    assert result.exit_code == 0, result.output
    assert observed_clean == [True]


def test_batch_content_competing_batch_driver_refuses_live_lease(
    runner: CliRunner,
    empty_sha256_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(tmp_path, monkeypatch)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner, empty_sha256_root, staging, "obj:driver", ("a.txt", b"a")
    )
    marker = tmp_path / "driver-ready"
    release = tmp_path / "driver-release"
    first, first_out = _start_batch_process(
        empty_sha256_root,
        staging,
        ("timestamp", "batch", "content", "--object", "obj:driver"),
        marker=marker,
        release=release,
    )
    _wait_for_path(marker)
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))

    second, second_out = _start_batch_process(
        empty_sha256_root,
        staging,
        ("timestamp", "batch", "content", "--batch", batch_id),
    )
    second_code, second_output = _process_result(second, second_out)
    release.write_text("go")
    first_code, first_output = _process_result(first, first_out)

    assert first_code == 0, first_output
    assert second_code == 4  # LockTimeoutError: ErrorClass.BUSY
    assert "driver lease is held" in second_output
    batch = batch_state.load_batch(staging, batch_id)
    assert batch.submissions[0].status == "submitted"


def test_batch_content_competing_same_object_batch_skips_in_batch(
    runner: CliRunner,
    empty_sha256_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(tmp_path, monkeypatch)
    staging = tmp_path / "staging"
    _new_ots_with_files(runner, empty_sha256_root, staging, "obj:same", ("a.txt", b"a"))
    marker = tmp_path / "same-ready"
    release = tmp_path / "same-release"
    first, first_out = _start_batch_process(
        empty_sha256_root,
        staging,
        ("timestamp", "batch", "content", "--object", "obj:same"),
        marker=marker,
        release=release,
    )
    _wait_for_path(marker)

    second, second_out = _start_batch_process(
        empty_sha256_root,
        staging,
        ("timestamp", "batch", "content", "--object", "obj:same"),
    )
    second_code, second_output = _process_result(second, second_out)
    release.write_text("go")
    first_code, first_output = _process_result(first, first_out)

    assert first_code == 0, first_output
    assert second_code == 0, second_output
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
    _write_config(tmp_path, monkeypatch)
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
        ("timestamp", "batch", "content", "--object", "obj:left"),
        marker=left_marker,
        release=left_release,
    )
    right, right_out = _start_batch_process(
        empty_sha256_root,
        staging,
        ("timestamp", "batch", "content", "--object", "obj:right"),
        marker=right_marker,
        release=right_release,
    )
    _wait_for_path(left_marker)
    _wait_for_path(right_marker)
    left_release.write_text("go")
    right_release.write_text("go")
    left_code, left_output = _process_result(left, left_out)
    right_code, right_output = _process_result(right, right_out)

    assert left_code == 0, left_output
    assert right_code == 0, right_output
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
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, sp_fake)
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
            target="content",
            root_path=str(empty_sha256_root.resolve()),
            category_id=641,
            created_at="2026-05-12T10:00:00Z",
            updated_at="2026-05-12T10:00:00Z",
        ),
    )
    batch_lock_depth = 0
    events: list[str] = []
    original_batch_lock = batch_state.with_batch_lock
    original_object_lock = stage_commands.with_lock

    def recording_batch_lock(*args, **kwargs):
        manager = original_batch_lock(*args, **kwargs)

        class _RecordingBatchLock:
            def __enter__(self):
                nonlocal batch_lock_depth
                batch_lock_depth += 1
                events.append("batch-enter")
                return manager.__enter__()

            def __exit__(self, exc_type, exc, tb):
                nonlocal batch_lock_depth
                try:
                    return manager.__exit__(exc_type, exc, tb)
                finally:
                    events.append("batch-exit")
                    batch_lock_depth -= 1

        return _RecordingBatchLock()

    def recording_object_lock(*args, **kwargs):
        events.append(f"object-enter-while-batch={batch_lock_depth}")
        return original_object_lock(*args, **kwargs)

    monkeypatch.setattr(batch_state, "with_batch_lock", recording_batch_lock)
    monkeypatch.setattr(stage_commands, "with_lock", recording_object_lock)

    result = runner.invoke(
        app,
        _stage_args(
            empty_sha256_root,
            staging,
            "timestamp",
            "batch",
            "content",
            "--batch",
            batch_id,
        ),
    )

    assert result.exit_code == 0, result.output
    assert "object-enter-while-batch=0" in events
    assert "object-enter-while-batch=1" not in events
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
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner, empty_sha256_root, staging, "obj:empty", ("a.txt", b"a")
    )
    create = runner.invoke(
        app,
        _stage_args(
            empty_sha256_root,
            staging,
            "timestamp",
            "batch",
            "content",
            "--object",
            "obj:empty",
        ),
    )
    assert create.exit_code == 0, create.output
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
        _stage_args(
            empty_sha256_root,
            staging,
            "timestamp",
            "batch",
            "content",
            "--batch",
            batch_id,
        ),
    )

    assert resume.exit_code == 0, resume.output
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
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, sp_fake)
    monkeypatch.setattr(service, "_SUBMIT_CHUNK_SIZE", 2)
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
        _stage_args(
            empty_sha256_root,
            staging,
            "timestamp",
            "batch",
            "content",
            "--object",
            "obj:chunked",
        ),
    )

    assert result.exit_code == 0, result.output
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
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, fake)
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
        _stage_args(
            empty_sha256_root,
            staging,
            "timestamp",
            "batch",
            "content",
            "--object",
            "obj:transient",
        ),
    )

    assert result.exit_code == 0, result.output
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
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner, empty_sha256_root, staging, "obj:no-retry", ("a.txt", b"a")
    )
    create = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "timestamp",
        "batch",
        "content",
        "--object",
        "obj:no-retry",
    )
    assert create.exit_code == 0, create.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))
    assert len(sp_fake._submissions) == 1

    result = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "timestamp",
        "batch",
        "content",
        "--batch",
        batch_id,
        "--retry",
    )

    assert result.exit_code == 0, result.output
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
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, sp_fake)
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
        "timestamp",
        "batch",
        "content",
        "--object",
        "obj:bidirectional",
    )
    assert create.exit_code == 0, create.output
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
        "timestamp",
        "batch",
        "content",
        "--batch",
        batch_id,
        "--retry",
    )

    assert result.exit_code == 0, result.output
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
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, sp_fake)
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
        "timestamp",
        "batch",
        "content",
        "--object",
        "obj:partial-repair",
    )
    assert create.exit_code == 0, create.output
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
        "timestamp",
        "batch",
        "content",
        "--batch",
        batch_id,
        "--retry",
    )

    assert result.exit_code == 0, result.output
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
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, sp_fake)
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
            inventory_proof=InventoryProof(
                version="v1",
                digest=digest,
                batch_id=batch_id,
                record_id=None,
            ),
        ),
    )
    save_batch(
        staging,
        BatchFile(
            batch_id=batch_id,
            target="inventory",
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
        "timestamp",
        "batch",
        "inventory",
        "--batch",
        batch_id,
        "--retry",
    )

    assert result.exit_code == 0, result.output
    assert sp_fake._submissions == []
    updated = batch_state.load_batch(staging, batch_id)
    assert updated.objects[0].status == "submitted"
    state = load_ots(
        StagingStore(staging).staging_path(root_path, "obj:inventory-retry")
    )
    assert state.inventory_proof is not None
    assert state.inventory_proof.record_id == "batch-rec-inv"


def test_batch_retry_pending_chunk_posts_after_crash_before_post(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner, empty_sha256_root, staging, "obj:pending-retry", ("a.txt", b"a")
    )
    create = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "timestamp",
        "batch",
        "content",
        "--object",
        "obj:pending-retry",
    )
    assert create.exit_code == 0, create.output
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
        "timestamp",
        "batch",
        "content",
        "--batch",
        batch_id,
        "--retry",
    )

    assert result.exit_code == 0, result.output
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
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, sp_fake)
    staging = tmp_path / "staging"
    _new_ots_with_files(
        runner, empty_sha256_root, staging, "obj:retry-regressed", ("a.txt", b"a")
    )
    create = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "timestamp",
        "batch",
        "content",
        "--object",
        "obj:retry-regressed",
    )
    assert create.exit_code == 0, create.output
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
        "timestamp",
        "batch",
        "content",
        "--batch",
        batch_id,
        "--retry",
    )

    assert result.exit_code == 0, result.output
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
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, sp_fake)
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
            target="content",
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
        "timestamp",
        "batch",
        "content",
        "--batch",
        batch_id,
        "--retry",
    )

    assert result.exit_code == 0, result.output
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
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "batch_file_missing"
    assert error["exit"] == 2
    assert "stage timestamp content/inventory" in error["message"]


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


def test_batch_content_upgrade_posts_then_ingests_per_digest(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(tmp_path, monkeypatch)
    btc = FakeBitcoinRPC()
    _inject_fake_sp(runner, sp_fake)
    _inject_fake_btc(runner, btc)
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
        "timestamp",
        "batch",
        "content",
        "--object",
        "obj:batch-upgrade",
    )
    assert submitted.exit_code == 0, submitted.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))
    batch = batch_state.load_batch(staging, batch_id)
    digest = batch.objects[0].digests[0]

    first = _invoke_json(
        runner,
        empty_sha256_root,
        staging,
        "timestamp",
        "batch",
        "content",
        "--batch",
        batch_id,
        "--upgrade",
    )
    assert first.exit_code == 0, first.output
    events = [json.loads(line) for line in first.output.splitlines()]
    assert [event["event"] for event in events] == [
        "batch_resumed",
        "object_submitted",
        "summary",
    ]
    assert events[1]["next"] == "Re-run --upgrade later"
    batch = batch_state.load_batch(staging, batch_id)
    assert len(batch.verify_jobs) == 1
    assert batch.verify_jobs[0].digests == (digest,)
    _assert_z_timestamp(batch.verify_jobs[0].submitted_at)
    assert batch.objects[0].status == "submitted"

    ots_bytes = _ots_for(digest)
    sp_fake.advance_to_completed(batch.verify_jobs[0].job_id, {digest: ots_bytes})
    _grow_chain_to(btc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
    _register_attested(btc, digest)

    second = _invoke_json(
        runner,
        empty_sha256_root,
        staging,
        "timestamp",
        "batch",
        "content",
        "--batch",
        batch_id,
        "--upgrade",
    )
    assert second.exit_code == 0, second.output
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
    assert state.content_proofs[digest].verified is True
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
    _write_config(tmp_path, monkeypatch)
    btc = FakeBitcoinRPC()
    _inject_fake_sp(runner, sp_fake)
    _inject_fake_btc(runner, btc)
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
        "timestamp",
        "batch",
        "content",
        "--object",
        "obj:batch-partial-results",
    )
    assert submitted.exit_code == 0, submitted.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))

    first = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "timestamp",
        "batch",
        "content",
        "--batch",
        batch_id,
        "--upgrade",
    )
    assert first.exit_code == 0, first.output
    batch = batch_state.load_batch(staging, batch_id)
    assert len(batch.verify_jobs) == 1
    assert len(batch.verify_jobs[0].digests) == 2
    returned_digest, missing_digest = batch.verify_jobs[0].digests

    sp_fake.advance_to_completed(
        batch.verify_jobs[0].job_id, {returned_digest: _ots_for(returned_digest)}
    )
    _grow_chain_to(btc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
    _register_attested(btc, returned_digest)

    partial = _invoke_json(
        runner,
        empty_sha256_root,
        staging,
        "timestamp",
        "batch",
        "content",
        "--batch",
        batch_id,
        "--upgrade",
    )
    assert partial.exit_code == 0, partial.output
    events = [json.loads(line) for line in partial.output.splitlines()]
    assert events[1]["event"] == "object_submitted"
    assert events[1]["last_error"] == "sp_partial_results"
    assert events[1]["next"] == "Re-run --upgrade later"
    batch = batch_state.load_batch(staging, batch_id)
    assert batch.objects[0].status == "submitted"
    assert batch.objects[0].last_error == "sp_partial_results"
    state = load_ots(
        StagingStore(staging).staging_path(
            str(empty_sha256_root.resolve()), "obj:batch-partial-results"
        )
    )
    assert state.content_proofs[returned_digest].verified is True
    missing_proof = state.content_proofs.get(missing_digest)
    assert missing_proof is None or missing_proof.verified is False

    retry = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "timestamp",
        "batch",
        "content",
        "--batch",
        batch_id,
        "--upgrade",
    )
    assert retry.exit_code == 0, retry.output
    batch = batch_state.load_batch(staging, batch_id)
    assert [job.digests for job in batch.verify_jobs] == [
        (returned_digest, missing_digest),
        (missing_digest,),
    ]

    sp_fake.advance_to_completed(
        batch.verify_jobs[1].job_id, {missing_digest: _ots_for(missing_digest)}
    )
    _register_attested(btc, missing_digest)
    completed = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "timestamp",
        "batch",
        "content",
        "--batch",
        batch_id,
        "--upgrade",
    )
    assert completed.exit_code == 0, completed.output
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
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, sp_fake)
    _inject_fake_btc(runner, FakeBitcoinRPC())
    from ocfl_cli.stage import watch as stage_watch

    monkeypatch.setattr(stage_watch, "_sleep_after_block", lambda: None, raising=True)
    subscribers = 0

    class _InterruptingStream:
        def wait_for_block(self, timeout_ms: int) -> bytes | None:
            raise KeyboardInterrupt

    def subscriber_factory(*args, **kwargs):
        nonlocal subscribers
        subscribers += 1
        return _InterruptingStream()

    monkeypatch.setattr(
        stage_watch, "_get_zmq_subscriber", subscriber_factory, raising=True
    )
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
        "timestamp",
        "batch",
        "content",
        "--object",
        "obj:batch-watch",
        "--watch",
    )

    assert result.exit_code == 130, result.output
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
    _write_config(tmp_path, monkeypatch)
    btc = FakeBitcoinRPC()
    _inject_fake_sp(runner, sp_fake)
    _inject_fake_btc(runner, btc)
    from ocfl_cli.stage import watch as stage_watch

    monkeypatch.setattr(stage_watch, "_sleep_after_block", lambda: None, raising=True)

    class _OneBlockStream:
        seen = False

        def wait_for_block(self, timeout_ms: int) -> bytes | None:
            if not self.seen:
                self.seen = True
                btc.extend(1)
                return bytes.fromhex(btc.getblockhash(btc.tip_height))
            raise KeyboardInterrupt

    monkeypatch.setattr(
        stage_watch, "_get_zmq_subscriber", lambda *a, **kw: _OneBlockStream()
    )
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
        "timestamp",
        "batch",
        "content",
        "--object",
        "obj:batch-watch-block",
        "--watch",
    )

    assert result.exit_code == 130, result.output
    assert f"block 1 {stage_commands._short_block_hash(btc.getblockhash(1))}" in (
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
    _write_config(tmp_path, monkeypatch)
    btc = FakeBitcoinRPC()
    _inject_fake_sp(runner, sp_fake)
    _inject_fake_btc(runner, btc)
    from ocfl_cli.stage import watch as stage_watch

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
                    batch.verify_jobs[0].job_id, {digest: _ots_for(digest)}
                )
                _grow_chain_to(btc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
                _register_attested(btc, digest)
            if self.timeouts > 3:
                raise KeyboardInterrupt
            return None

    def subscriber_factory(*args, **kwargs):
        nonlocal subscribers
        subscribers += 1
        return _QuietCompletingStream()

    monkeypatch.setattr(
        stage_watch, "_get_zmq_subscriber", subscriber_factory, raising=True
    )

    result = _invoke_json(
        runner,
        empty_sha256_root,
        staging,
        "timestamp",
        "batch",
        "content",
        "--object",
        "obj:batch-watch-quiet",
        "--watch",
    )

    assert result.exit_code == 0, result.output
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
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, TransientSubmitFake())
    _inject_fake_btc(runner, FakeBitcoinRPC())
    from ocfl_cli.stage import watch as stage_watch

    monkeypatch.setattr(
        stage_watch,
        "_get_zmq_subscriber",
        lambda *a, **kw: pytest.fail("watch subscribed with no submitted rows"),
        raising=True,
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
        "timestamp",
        "batch",
        "content",
        "--object",
        "obj:batch-watch-empty",
        "--watch",
    )

    assert result.exit_code == 1, result.output
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
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, sp_fake)
    _inject_fake_btc(runner, FakeBitcoinRPC())
    from ocfl_cli.stage import watch as stage_watch

    monkeypatch.setattr(
        stage_watch,
        "_get_zmq_subscriber",
        lambda *a, **kw: pytest.fail("watch subscribed with only skipped rows"),
        raising=True,
    )
    staging = tmp_path / "staging"

    result = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "timestamp",
        "batch",
        "content",
        "--object",
        "obj:not-staged",
        "--watch",
    )

    assert result.exit_code == 1, result.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))
    batch = batch_state.load_batch(staging, batch_id)
    assert batch.objects[0].status == "skipped"


def test_batch_watch_enters_upgrade_for_submitted_subset_after_submit_failure(
    runner: CliRunner,
    empty_sha256_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(tmp_path, monkeypatch)
    sp = SecondSubmitTransientFake()
    _inject_fake_sp(runner, sp)
    _inject_fake_btc(runner, FakeBitcoinRPC())
    from ocfl_cli.stage import service as stage_service
    from ocfl_cli.stage import watch as stage_watch

    monkeypatch.setattr(stage_service, "_SUBMIT_CHUNK_SIZE", 1, raising=True)
    monkeypatch.setattr(stage_watch, "_sleep_after_block", lambda: None, raising=True)

    class _InterruptingStream:
        def wait_for_block(self, timeout_ms: int) -> bytes | None:
            raise KeyboardInterrupt

    _inject_fake_zmq(monkeypatch, _InterruptingStream())
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
        "timestamp",
        "batch",
        "content",
        "--object",
        "obj:watch-partial-a",
        "--object",
        "obj:watch-partial-b",
        "--watch",
    )

    assert result.exit_code == 130, result.output
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
    _write_config(tmp_path, monkeypatch)
    btc = FakeBitcoinRPC()
    _inject_fake_sp(runner, sp_fake)
    _inject_fake_btc(runner, btc)
    from ocfl_cli.stage import commands as stage_commands
    from ocfl_cli.stage import watch as stage_watch

    monkeypatch.setattr(stage_watch, "_sleep_after_block", lambda: None, raising=True)
    calls: list[str] = []

    def mismatch_once(*args, **kwargs):
        calls.append(kwargs["expected_digest"].hex())
        raise AttestationMismatchError("stale proof")

    monkeypatch.setattr(
        stage_commands, "verify_attestation", mismatch_once, raising=True
    )
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
                    batch_id=batch_id,
                    record_ids=("rec-a", "rec-b"),
                ),
            ),
            content_proofs={
                verified_digest: VerifiedProof(
                    ots_path=f"ots-cache/{verified_digest}.ots",
                    verified=True,
                )
            },
        ),
    )
    ots_cache_path(staging_dir, verified_digest).parent.mkdir(parents=True)
    ots_cache_path(staging_dir, verified_digest).write_bytes(b"cached-ots")
    save_batch(
        staging,
        BatchFile(
            batch_id=batch_id,
            target="content",
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
                btc.extend(5)
                return bytes.fromhex(btc.getblockchaininfo().best_block_hash)
            raise KeyboardInterrupt

    _inject_fake_zmq(monkeypatch, _JumpStream())

    result = _invoke(
        runner,
        storage_root,
        staging,
        "timestamp",
        "batch",
        "content",
        "--batch",
        batch_id,
        "--watch",
    )

    assert result.exit_code == 130, result.output
    assert calls == [verified_digest]
    batch = batch_state.load_batch(staging, batch_id)
    assert len(sp_fake._jobs) == 2
    assert any(job.digests == (verified_digest,) for job in batch.verify_jobs)
    state = load_ots(staging_dir)
    assert state.content_proofs[verified_digest].verified is False


def test_batch_watch_revalidates_mixed_upgraded_content_row(
    runner: CliRunner,
    storage_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(tmp_path, monkeypatch)
    btc = FakeBitcoinRPC()
    _inject_fake_sp(runner, sp_fake)
    _inject_fake_btc(runner, btc)
    from ocfl_cli.stage import commands as stage_commands
    from ocfl_cli.stage import watch as stage_watch

    monkeypatch.setattr(stage_watch, "_sleep_after_block", lambda: None, raising=True)
    calls: list[str] = []

    def mismatch_once(*args, **kwargs):
        calls.append(kwargs["expected_digest"].hex())
        raise AttestationMismatchError("stale proof")

    monkeypatch.setattr(
        stage_commands, "verify_attestation", mismatch_once, raising=True
    )
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
                    batch_id=batch_id,
                    record_ids=("rec-upgraded",),
                ),
            ),
            content_proofs={
                upgraded_digest: VerifiedProof(
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
                    batch_id=batch_id,
                    record_ids=("rec-waiting",),
                ),
            ),
        ),
    )
    ots_cache_path(upgraded_dir, upgraded_digest).parent.mkdir(parents=True)
    ots_cache_path(upgraded_dir, upgraded_digest).write_bytes(b"cached-ots")
    save_batch(
        staging,
        BatchFile(
            batch_id=batch_id,
            target="content",
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
                btc.extend(5)
                return bytes.fromhex(btc.getblockchaininfo().best_block_hash)
            raise KeyboardInterrupt

    _inject_fake_zmq(monkeypatch, _JumpStream())

    result = _invoke(
        runner,
        storage_root,
        staging,
        "timestamp",
        "batch",
        "content",
        "--batch",
        batch_id,
        "--watch",
    )

    assert result.exit_code == 130, result.output
    assert calls == [upgraded_digest]
    batch = batch_state.load_batch(staging, batch_id)
    rows = {row.object_id: row for row in batch.objects}
    assert rows["obj:watch-upgraded"].status == "submitted"
    assert rows["obj:watch-waiting"].status == "submitted"
    assert any(job.digests == (waiting_digest,) for job in batch.verify_jobs)
    assert any(job.digests == (upgraded_digest,) for job in batch.verify_jobs)
    upgraded_state = load_ots(upgraded_dir)
    assert upgraded_state.phase is Phase.CONTENT_PENDING
    assert upgraded_state.content_proofs[upgraded_digest].verified is False
    assert load_ots(waiting_dir).phase is Phase.CONTENT_PENDING


def test_batch_watch_revalidates_mixed_upgraded_inventory_row(
    runner: CliRunner,
    storage_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(tmp_path, monkeypatch)
    btc = FakeBitcoinRPC()
    _inject_fake_sp(runner, sp_fake)
    _inject_fake_btc(runner, btc)
    from ocfl_cli.stage import commands as stage_commands
    from ocfl_cli.stage import watch as stage_watch

    monkeypatch.setattr(stage_watch, "_sleep_after_block", lambda: None, raising=True)
    calls: list[str] = []

    def mismatch_once(*args, **kwargs):
        calls.append(kwargs["expected_digest"].hex())
        raise AttestationMismatchError("stale inventory proof")

    monkeypatch.setattr(
        stage_commands, "verify_attestation", mismatch_once, raising=True
    )
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
            inventory_proof=InventoryProof(
                version="v1",
                digest=upgraded_digest,
                proof=VerifiedProof(
                    ots_path=f"ots-cache/{upgraded_digest}.ots",
                    verified=True,
                ),
                batch_id=batch_id,
                record_id="rec-upgraded",
            ),
        ),
    )
    waiting_dir = _ots_staging(
        staging,
        root_path,
        "obj:inventory-waiting",
        OtsState(
            phase=Phase.INVENTORY_PENDING,
            category_id=641,
            inventory_proof=InventoryProof(
                version="v1",
                digest=waiting_digest,
                proof=VerifiedProof(verified=False),
                batch_id=batch_id,
                record_id="rec-waiting",
            ),
        ),
    )
    ots_cache_path(upgraded_dir, upgraded_digest).parent.mkdir(parents=True)
    ots_cache_path(upgraded_dir, upgraded_digest).write_bytes(b"cached-ots")
    save_batch(
        staging,
        BatchFile(
            batch_id=batch_id,
            target="inventory",
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
                btc.extend(5)
                return bytes.fromhex(btc.getblockchaininfo().best_block_hash)
            raise KeyboardInterrupt

    _inject_fake_zmq(monkeypatch, _JumpStream())

    result = _invoke(
        runner,
        storage_root,
        staging,
        "timestamp",
        "batch",
        "inventory",
        "--batch",
        batch_id,
        "--watch",
    )

    assert result.exit_code == 130, result.output
    assert calls == [upgraded_digest]
    batch = batch_state.load_batch(staging, batch_id)
    rows = {row.object_id: row for row in batch.objects}
    assert rows["obj:inventory-upgraded"].status == "submitted"
    assert rows["obj:inventory-waiting"].status == "submitted"
    assert any(job.digests == (waiting_digest,) for job in batch.verify_jobs)
    assert any(job.digests == (upgraded_digest,) for job in batch.verify_jobs)
    upgraded_state = load_ots(upgraded_dir)
    assert upgraded_state.phase is Phase.INVENTORY_PENDING
    assert upgraded_state.inventory_proof is not None
    assert upgraded_state.inventory_proof.proof.verified is False
    assert load_ots(waiting_dir).phase is Phase.INVENTORY_PENDING


def test_batch_watch_content_reorg_revalidation_is_object_scoped(
    runner: CliRunner,
    storage_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(tmp_path, monkeypatch)
    btc = FakeBitcoinRPC()
    _inject_fake_sp(runner, sp_fake)
    _inject_fake_btc(runner, btc)
    from ocfl_cli.stage import commands as stage_commands
    from ocfl_cli.stage import watch as stage_watch

    monkeypatch.setattr(stage_watch, "_sleep_after_block", lambda: None, raising=True)

    def deep_reorg(*args, **kwargs):
        raise ReorgDetectedError("reorg deeper than confirmation depth")

    monkeypatch.setattr(stage_commands, "verify_attestation", deep_reorg, raising=True)
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
                    batch_id=batch_id,
                    record_ids=("rec-failed",),
                ),
            ),
            content_proofs={
                failed_digest: VerifiedProof(
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
                    batch_id=batch_id,
                    record_ids=("rec-waiting",),
                ),
            ),
        ),
    )
    ots_cache_path(failed_dir, failed_digest).parent.mkdir(parents=True)
    ots_cache_path(failed_dir, failed_digest).write_bytes(b"cached-ots")
    save_batch(
        staging,
        BatchFile(
            batch_id=batch_id,
            target="content",
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
                btc.extend(5)
                return bytes.fromhex(btc.getblockchaininfo().best_block_hash)
            raise KeyboardInterrupt

    _inject_fake_zmq(monkeypatch, _JumpStream())

    result = _invoke(
        runner,
        storage_root,
        staging,
        "timestamp",
        "batch",
        "content",
        "--batch",
        batch_id,
        "--watch",
    )

    assert result.exit_code == 130, result.output
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
    assert state.content_proofs[failed_digest].verified is False


def test_batch_watch_inventory_reorg_revalidation_is_object_scoped(
    runner: CliRunner,
    storage_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(tmp_path, monkeypatch)
    btc = FakeBitcoinRPC()
    _inject_fake_sp(runner, sp_fake)
    _inject_fake_btc(runner, btc)
    from ocfl_cli.stage import commands as stage_commands
    from ocfl_cli.stage import watch as stage_watch

    monkeypatch.setattr(stage_watch, "_sleep_after_block", lambda: None, raising=True)

    def deep_reorg(*args, **kwargs):
        raise ReorgDetectedError("reorg deeper than confirmation depth")

    monkeypatch.setattr(stage_commands, "verify_attestation", deep_reorg, raising=True)
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
            inventory_proof=InventoryProof(
                version="v1",
                digest=failed_digest,
                proof=VerifiedProof(
                    ots_path=f"ots-cache/{failed_digest}.ots",
                    verified=True,
                ),
                batch_id=batch_id,
                record_id="rec-failed",
            ),
        ),
    )
    _ots_staging(
        staging,
        root_path,
        "obj:inventory-reorg-waiting",
        OtsState(
            phase=Phase.INVENTORY_PENDING,
            category_id=641,
            inventory_proof=InventoryProof(
                version="v1",
                digest=waiting_digest,
                proof=VerifiedProof(verified=False),
                batch_id=batch_id,
                record_id="rec-waiting",
            ),
        ),
    )
    ots_cache_path(failed_dir, failed_digest).parent.mkdir(parents=True)
    ots_cache_path(failed_dir, failed_digest).write_bytes(b"cached-ots")
    save_batch(
        staging,
        BatchFile(
            batch_id=batch_id,
            target="inventory",
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
                btc.extend(5)
                return bytes.fromhex(btc.getblockchaininfo().best_block_hash)
            raise KeyboardInterrupt

    _inject_fake_zmq(monkeypatch, _JumpStream())

    result = _invoke(
        runner,
        storage_root,
        staging,
        "timestamp",
        "batch",
        "inventory",
        "--batch",
        batch_id,
        "--watch",
    )

    assert result.exit_code == 130, result.output
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
    assert state.inventory_proof.proof.verified is False


def test_batch_watch_node_halt_exits_operational_error(
    runner: CliRunner,
    empty_sha256_root: Path,
    sp_fake: FakeSimpleProof,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, sp_fake)
    _inject_fake_btc(runner, TransientBitcoinRPC())
    from ocfl_cli.stage import watch as stage_watch

    monkeypatch.setattr(stage_watch, "_sleep_after_block", lambda: None, raising=True)
    stream = InMemoryHashBlockStream()
    stream.push(b"\x00" * 32)
    _inject_fake_zmq(monkeypatch, stream)
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
        "timestamp",
        "batch",
        "content",
        "--object",
        "obj:batch-watch-halt",
        "--watch",
    )

    assert result.exit_code == 1, result.output
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
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, sp_fake)
    _inject_fake_btc(runner, FakeBitcoinRPC())
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
        "timestamp",
        "batch",
        "content",
        "--object",
        "obj:batch-failed-job",
    )
    assert submitted.exit_code == 0, submitted.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))

    first = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "timestamp",
        "batch",
        "content",
        "--batch",
        batch_id,
        "--upgrade",
    )
    assert first.exit_code == 0, first.output
    failed_job_id = batch_state.load_batch(staging, batch_id).verify_jobs[0].job_id
    sp_fake.fail_job(failed_job_id)

    failed = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "timestamp",
        "batch",
        "content",
        "--batch",
        batch_id,
        "--upgrade",
    )
    assert failed.exit_code == 0, failed.output
    batch = batch_state.load_batch(staging, batch_id)
    assert batch.verify_jobs[0].status == "FAILED"
    assert batch.objects[0].status == "submitted"
    assert batch.objects[0].last_error == "verify_job_failed"

    retried = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "timestamp",
        "batch",
        "content",
        "--batch",
        batch_id,
        "--upgrade",
    )
    assert retried.exit_code == 0, retried.output
    batch = batch_state.load_batch(staging, batch_id)
    assert [job.status for job in batch.verify_jobs] == ["FAILED", "IN_PROGRESS"]
    assert len(sp_fake._jobs) == 2


def test_batch_upgrade_poll_transient_marks_retryable(
    runner: CliRunner,
    empty_sha256_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(tmp_path, monkeypatch)
    sp = TransientPollFake()
    _inject_fake_sp(runner, sp)
    _inject_fake_btc(runner, FakeBitcoinRPC())
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
        "timestamp",
        "batch",
        "content",
        "--object",
        "obj:batch-poll-fails",
    )
    assert submitted.exit_code == 0, submitted.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))

    upgraded = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "timestamp",
        "batch",
        "content",
        "--batch",
        batch_id,
        "--upgrade",
    )

    assert upgraded.exit_code == 0, upgraded.output
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
    _write_config(tmp_path, monkeypatch)
    sp = MismatchDownloadFake()
    btc = FakeBitcoinRPC()
    _inject_fake_sp(runner, sp)
    _inject_fake_btc(runner, btc)
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
        "timestamp",
        "batch",
        "content",
        "--object",
        "obj:batch-mismatch",
    )
    assert submitted.exit_code == 0, submitted.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))

    first = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "timestamp",
        "batch",
        "content",
        "--batch",
        batch_id,
        "--upgrade",
    )
    assert first.exit_code == 0, first.output
    batch = batch_state.load_batch(staging, batch_id)
    sp.advance_to_completed(batch.verify_jobs[0].job_id, {})

    mismatched = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "timestamp",
        "batch",
        "content",
        "--batch",
        batch_id,
        "--upgrade",
    )

    assert mismatched.exit_code == 0, mismatched.output
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
    _write_config(tmp_path, monkeypatch)
    _inject_fake_sp(runner, sp_fake)
    _inject_fake_btc(runner, TransientBitcoinRPC())
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
        "timestamp",
        "batch",
        "content",
        "--object",
        "obj:batch-rpc-fails",
    )
    assert submitted.exit_code == 0, submitted.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))

    first = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "timestamp",
        "batch",
        "content",
        "--batch",
        batch_id,
        "--upgrade",
    )
    assert first.exit_code == 0, first.output
    batch = batch_state.load_batch(staging, batch_id)
    digest = batch.objects[0].digests[0]
    sp_fake.advance_to_completed(
        batch.verify_jobs[0].job_id, {digest: _ots_for(digest)}
    )

    failed = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "timestamp",
        "batch",
        "content",
        "--batch",
        batch_id,
        "--upgrade",
    )

    assert failed.exit_code == 0, failed.output
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
    _write_config(tmp_path, monkeypatch)
    sp = ExpiredPollFake()
    _inject_fake_sp(runner, sp)
    _inject_fake_btc(runner, FakeBitcoinRPC())
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
        "timestamp",
        "batch",
        "content",
        "--object",
        "obj:batch-expired",
    )
    assert submitted.exit_code == 0, submitted.output
    batch_id = next(p.stem for p in (staging / "batches").glob("*.json"))

    first = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "timestamp",
        "batch",
        "content",
        "--batch",
        batch_id,
        "--upgrade",
    )
    assert first.exit_code == 0, first.output
    assert batch_state.load_batch(staging, batch_id).verify_jobs[0].status == "EXPIRED"

    second = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "timestamp",
        "batch",
        "content",
        "--batch",
        batch_id,
        "--upgrade",
    )

    assert second.exit_code == 0, second.output
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
    _write_config(tmp_path, monkeypatch)
    btc = FakeBitcoinRPC()
    _inject_fake_sp(runner, sp_fake)
    _inject_fake_btc(runner, btc)
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
            content_proofs={digest: VerifiedProof(verified=True)},
        ),
    )

    submitted = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "timestamp",
        "batch",
        "inventory",
        "--object",
        "obj:inv-stale",
        "--allow-missing-metadata",
    )
    assert submitted.exit_code == 0, submitted.output
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
        "timestamp",
        "batch",
        "inventory",
        "--batch",
        batch_id,
        "--upgrade",
    )
    assert first.exit_code == 0, first.output
    batch = batch_state.load_batch(staging, batch_id)
    ots_bytes = _ots_for(inv_digest)
    sp_fake.advance_to_completed(batch.verify_jobs[0].job_id, {inv_digest: ots_bytes})
    _grow_chain_to(btc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
    _register_attested(btc, inv_digest)

    upgraded = _invoke(
        runner,
        empty_sha256_root,
        staging,
        "timestamp",
        "batch",
        "inventory",
        "--batch",
        batch_id,
        "--upgrade",
    )

    assert upgraded.exit_code == 0, upgraded.output
    batch = batch_state.load_batch(staging, batch_id)
    assert batch.objects[0].status == "failed"
    assert batch.objects[0].last_error == "regressed"
    state = load_ots(staging_dir)
    assert state.inventory_proof is not None
    assert state.inventory_proof.proof.verified is False


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
