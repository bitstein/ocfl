from typing import TYPE_CHECKING, Protocol

from ocfl_cli.errors import (
    AttestationMismatchError,
    BatchMismatchError,
    CommitFailedError,
    ErrorContext,
    InventoryProofMissingError,
    LockTimeoutError,
    MissingStagedMetadataError,
    PhaseError,
    ReorgDetectedError,
    StagingFileChangedError,
    StagingFileMissingError,
    UsageError,
)
from ocfl_cli.model import batch as batch_model
from ocfl_cli.store import batch as batch_state
from ocfl_cli.workflow import commit as wf_commit
from ocfl_cli.workflow import watch as wf_watch
from ocfl_cli.workflow.batch.prepare import (
    collect_recoverable_object_intents,
    merge_recovered_object_intents,
    prepare_batch_object,
    replace_batch_object,
)
from ocfl_cli.workflow.batch.report import (
    CommitCountsOutput,
    CommitObjectEventOutput,
    CommitSummaryEventOutput,
    commit_object_event,
    commit_summary_event,
    load_batch_or_usage,
)
from ocfl_cli.workflow.batch.retry import retry_batch_submissions
from ocfl_cli.workflow.batch.submit import submit_batch
from ocfl_cli.workflow.batch.upgrade import run_batch_upgrade_pass
from ocfl_cli.workflow.batch.watch import (
    batch_has_submitted_subset,
    batch_watch_successful,
    reverify_batch_watch_rows,
)
from ocfl_cli.workflow.targets import rules_for
from ocfl_lib import BitcoinRpcError

if TYPE_CHECKING:
    from collections.abc import Callable

    from ocfl_cli.config import Config
    from ocfl_cli.model.phase import TimestampTarget
    from ocfl_cli.ports import BtcRpcFactory, Runtime, SpClientFactory
    from ocfl_cli.store.staging import StagingStore
    from ocfl_lib import StorageRoot
    from ocfl_lib.fs import WriteFS


class BatchEvents(Protocol):
    def created(self, batch: batch_model.BatchFile) -> None: ...

    def resumed(self, batch: batch_model.BatchFile) -> None: ...

    def object_prepared(
        self, batch: batch_model.BatchFile, row: batch_model.BatchObject
    ) -> None: ...

    def changed(
        self, before: batch_model.BatchFile, after: batch_model.BatchFile
    ) -> None: ...

    def waiting(self, batch: batch_model.BatchFile) -> None: ...

    def block(
        self,
        before: batch_model.BatchFile,
        after: batch_model.BatchFile,
        *,
        height: int,
        block_hash: str,
        reverified: bool,
    ) -> None: ...

    def watch_done(self, batch: batch_model.BatchFile) -> None: ...

    def commit_event(
        self, data: CommitObjectEventOutput | CommitSummaryEventOutput
    ) -> None: ...


def _new_or_resumed(
    store: StagingStore,
    *,
    target: TimestampTarget | None,
    root_path: str,
    selected: list[str],
    batch_id: str | None,
    category: int | None,
    default_category: int,
    runtime: Runtime,
) -> tuple[batch_model.BatchFile, bool]:
    if batch_id is not None:
        return load_batch_or_usage(store.root, batch_id), False
    if target is None:
        raise RuntimeError("a new batch needs a target")
    if not selected:
        raise UsageError("a new batch needs OBJECT... or --objects-file")
    now = batch_state.utc_iso_z(runtime.clock.now())
    batch = batch_model.BatchFile(
        batch_id=batch_state.new_batch_id(),
        target=target,
        root_path=root_path,
        category_id=category if category is not None else default_category,
        created_at=now,
        updated_at=now,
    )
    return batch, True


def _start(
    store: StagingStore,
    cfg: Config,
    sr: StorageRoot,
    *,
    target: TimestampTarget,
    root_path: str,
    batch: batch_model.BatchFile,
    created: bool,
    selected: list[str],
    allow_missing_metadata: bool,
    sp_factory: SpClientFactory,
    events: BatchEvents,
    runtime: Runtime,
) -> batch_model.BatchFile:
    staging_root = store.root
    if created:
        batch_state.save_batch(staging_root, batch)
        events.created(batch)
        for object_id in dict.fromkeys(selected):
            row = prepare_batch_object(
                target=target,
                cfg=cfg,
                store=store,
                sr=sr,
                root_path=root_path,
                staging_root=staging_root,
                batch=batch,
                object_id=object_id,
                allow_missing_metadata=allow_missing_metadata,
                runtime=runtime,
            )
            batch = replace_batch_object(batch, row, runtime=runtime)
            batch_state.save_batch(staging_root, batch)
            events.object_prepared(batch, row)
    else:
        events.resumed(batch)
    recovered = collect_recoverable_object_intents(store, batch, runtime=runtime)
    before_recovered = batch
    batch = batch_state.load_batch(staging_root, batch.batch_id)
    batch = merge_recovered_object_intents(batch, recovered, runtime=runtime)
    batch_state.save_batch(staging_root, batch)
    events.changed(before_recovered, batch)
    before_submit = batch
    batch = submit_batch(
        target=target,
        cfg=cfg,
        store=store,
        staging_root=staging_root,
        batch=batch,
        sp_factory=sp_factory,
        runtime=runtime,
    )
    events.changed(before_submit, batch)
    return batch


def submit(
    store: StagingStore,
    cfg: Config,
    sr: StorageRoot,
    *,
    target: TimestampTarget | None,
    root_path: str,
    selected: list[str],
    batch_id: str | None,
    category: int | None,
    allow_missing_metadata: bool,
    sp_factory: SpClientFactory,
    events: BatchEvents,
    runtime: Runtime,
) -> batch_model.BatchFile:
    sp_cfg = cfg.simpleproof
    if sp_cfg is None:
        raise RuntimeError("simpleproof config missing")
    batch, created = _new_or_resumed(
        store,
        target=target,
        root_path=root_path,
        selected=selected,
        batch_id=batch_id,
        category=category,
        default_category=sp_cfg.proof_category_id,
        runtime=runtime,
    )
    with batch_state.batch_lock(store.root, batch.batch_id, runtime):
        return _start(
            store,
            cfg,
            sr,
            target=batch.target,
            root_path=root_path,
            batch=batch,
            created=created,
            selected=selected,
            allow_missing_metadata=allow_missing_metadata,
            sp_factory=sp_factory,
            events=events,
            runtime=runtime,
        )


def retry(
    store: StagingStore,
    cfg: Config,
    *,
    batch_id: str,
    sp_factory: SpClientFactory,
    events: BatchEvents,
    runtime: Runtime,
) -> batch_model.BatchFile:
    staging_root = store.root
    batch = load_batch_or_usage(staging_root, batch_id)
    target = batch.target
    with batch_state.batch_lock(staging_root, batch.batch_id, runtime):
        events.resumed(batch)
        before_retry = batch
        batch = retry_batch_submissions(
            target=target,
            cfg=cfg,
            store=store,
            staging_root=staging_root,
            batch=batch,
            sp_factory=sp_factory,
            runtime=runtime,
        )
        events.changed(before_retry, batch)
    return batch


def upgrade(
    store: StagingStore,
    cfg: Config,
    open_root: Callable[[], StorageRoot],
    *,
    batch_id: str,
    sp_factory: SpClientFactory,
    btc_factory: BtcRpcFactory,
    events: BatchEvents,
    runtime: Runtime,
) -> batch_model.BatchFile:
    staging_root = store.root
    batch = load_batch_or_usage(staging_root, batch_id)
    target = batch.target
    sr = open_root()
    with batch_state.batch_lock(staging_root, batch.batch_id, runtime):
        events.resumed(batch)
        before_upgrade = batch
        batch = run_batch_upgrade_pass(
            target=target,
            cfg=cfg,
            store=store,
            sr=sr,
            staging_root=staging_root,
            batch=batch,
            sp_factory=sp_factory,
            btc_factory=btc_factory,
            runtime=runtime,
        )
        events.changed(before_upgrade, batch)
    return batch


def watch(
    store: StagingStore,
    cfg: Config,
    sr: StorageRoot,
    *,
    target: TimestampTarget | None,
    root_path: str,
    selected: list[str],
    batch_id: str | None,
    category: int | None,
    allow_missing_metadata: bool,
    sp_factory: SpClientFactory,
    btc_factory: BtcRpcFactory,
    events: BatchEvents,
    runtime: Runtime,
) -> int:
    sp_cfg = cfg.simpleproof
    if sp_cfg is None:
        raise RuntimeError("simpleproof config missing")
    if cfg.bitcoin is None:
        raise RuntimeError("bitcoin config missing")
    staging_root = store.root
    batch, created = _new_or_resumed(
        store,
        target=target,
        root_path=root_path,
        selected=selected,
        batch_id=batch_id,
        category=category,
        default_category=sp_cfg.proof_category_id,
        runtime=runtime,
    )
    target = batch.target
    exit_code = 1

    def run_watch_upgrade_pass(
        batch: batch_model.BatchFile, *, reload_batch: bool = False
    ) -> tuple[batch_model.BatchFile, bool]:
        nonlocal exit_code
        if reload_batch:
            batch = batch_state.load_batch(staging_root, batch.batch_id)
        before_upgrade = batch
        batch = run_batch_upgrade_pass(
            target=target,
            cfg=cfg,
            store=store,
            sr=sr,
            staging_root=staging_root,
            batch=batch,
            sp_factory=sp_factory,
            btc_factory=btc_factory,
            runtime=runtime,
        )
        events.changed(before_upgrade, batch)
        if batch_has_submitted_subset(batch):
            return batch, False
        exit_code = 0 if batch_watch_successful(batch) else 1
        return batch, True

    try:
        with batch_state.batch_lock(staging_root, batch.batch_id, runtime):
            batch = _start(
                store,
                cfg,
                sr,
                target=target,
                root_path=root_path,
                batch=batch,
                created=created,
                selected=selected,
                allow_missing_metadata=allow_missing_metadata,
                sp_factory=sp_factory,
                events=events,
                runtime=runtime,
            )
            batch, done = run_watch_upgrade_pass(batch)
            if done:
                return exit_code

            events.waiting(batch)
            btc = btc_factory(cfg)
            last_height: int | None = None
            last_tip_hash: str | None = None
            with runtime.block_streams(cfg) as zmq_sub:
                while True:
                    block = zmq_sub.wait_for_block(timeout_ms=5000)
                    if block is None:
                        batch, done = run_watch_upgrade_pass(batch, reload_batch=True)
                        if done:
                            return exit_code
                        continue
                    runtime.sleeper.sleep(wf_watch.BLOCK_PROCESSING_DELAY_SECONDS)
                    info = btc.getblockchaininfo()
                    height_jumped = (
                        last_height is not None and info.height - last_height > 1
                    )
                    tip_changed_at_stable = (
                        last_height is not None
                        and info.height == last_height
                        and last_tip_hash is not None
                        and last_tip_hash != info.best_block_hash
                    )
                    last_height = info.height
                    last_tip_hash = info.best_block_hash
                    batch = batch_state.load_batch(staging_root, batch.batch_id)
                    before_reverify = batch
                    if height_jumped or tip_changed_at_stable:
                        batch = reverify_batch_watch_rows(
                            target=target,
                            cfg=cfg,
                            store=store,
                            staging_root=staging_root,
                            batch=batch,
                            btc=btc,
                            runtime=runtime,
                        )
                        events.changed(before_reverify, batch)
                    before_block_upgrade = batch
                    batch, done = run_watch_upgrade_pass(batch)
                    events.block(
                        before_block_upgrade,
                        batch,
                        height=info.height,
                        block_hash=info.best_block_hash,
                        reverified=height_jumped or tip_changed_at_stable,
                    )
                    if done:
                        return exit_code
    except KeyboardInterrupt:
        return 130
    except (BitcoinRpcError, ReorgDetectedError) as e:
        raise ErrorContext(e, prefix="halt: ") from e
    finally:
        events.watch_done(batch)


def commit_failure_reason(exc: Exception) -> str:
    if isinstance(exc, AttestationMismatchError):
        return "attestation_mismatch"
    if isinstance(exc, (PhaseError, InventoryProofMissingError)):
        return "phase_error"
    if isinstance(exc, MissingStagedMetadataError):
        return "missing_metadata"
    if isinstance(exc, StagingFileMissingError):
        return "staging_file_missing"
    if isinstance(exc, StagingFileChangedError):
        return "staging_file_changed"
    if isinstance(exc, CommitFailedError):
        return "commit_failed"
    if isinstance(exc, LockTimeoutError):
        return "lock_contended"
    raise TypeError(f"unsupported batch commit exception {type(exc).__name__}")


def commit(
    store: StagingStore,
    open_root: Callable[[], tuple[StorageRoot, WriteFS]],
    *,
    root_path: str,
    batch_id: str,
    events: BatchEvents,
    runtime: Runtime,
    commit_object: Callable[..., None] = wf_commit.commit_ots,
) -> int:
    staging_root = store.root
    batch = load_batch_or_usage(staging_root, batch_id)
    with batch_state.batch_lock(staging_root, batch.batch_id, runtime):
        batch = load_batch_or_usage(staging_root, batch.batch_id)
        if not rules_for(batch.target).commits:
            raise BatchMismatchError(
                f"commit batches must target inventory; got {batch.target.value}"
            )
        if batch.root_path != root_path:
            raise BatchMismatchError(
                f"batch {batch.batch_id} belongs to root {batch.root_path}; "
                f"current root is {root_path}"
            )
        snapshot = tuple(
            (row.object_id, row.status, row.skipped_reason) for row in batch.objects
        )

        sr, fs = open_root()
        counts: CommitCountsOutput = {
            "committed": 0,
            "skipped": 0,
            "failed": 0,
            "total": len(snapshot),
        }
        failures = 0
        for object_id, status, skipped_reason in snapshot:
            staging_dir = store.staging_path(batch.root_path, object_id)
            if not staging_dir.exists():
                counts["skipped"] += 1
                events.commit_event(
                    commit_object_event(
                        batch=batch,
                        object_id=object_id,
                        event="object_skipped",
                        reason="committed",
                    )
                )
                continue
            if status != "upgraded":
                counts["skipped"] += 1
                events.commit_event(
                    commit_object_event(
                        batch=batch,
                        object_id=object_id,
                        event="object_skipped",
                        reason=skipped_reason or status,
                    )
                )
                continue
            try:
                commit_object(
                    store=store,
                    sr=sr,
                    fs=fs,
                    root_path=batch.root_path,
                    object_id=object_id,
                    staging_dir=staging_dir,
                    allow_missing_metadata=False,
                    runtime=runtime,
                )
            except (
                AttestationMismatchError,
                PhaseError,
                InventoryProofMissingError,
                MissingStagedMetadataError,
                StagingFileMissingError,
                StagingFileChangedError,
                CommitFailedError,
                LockTimeoutError,
            ) as e:
                counts["failed"] += 1
                failures += 1
                events.commit_event(
                    commit_object_event(
                        batch=batch,
                        object_id=object_id,
                        event="object_failed",
                        reason=commit_failure_reason(e),
                        message=str(e),
                    )
                )
                continue
            counts["committed"] += 1
            events.commit_event(
                commit_object_event(
                    batch=batch,
                    object_id=object_id,
                    event="object_committed",
                )
            )
        events.commit_event(commit_summary_event(batch, counts))
        return failures
