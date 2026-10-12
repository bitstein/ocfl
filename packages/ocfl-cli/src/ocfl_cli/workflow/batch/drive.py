"""Batch use cases: create or resume a batch and drive it, reporting events.

Each driver holds the batch's driver lease while it runs and reports what
changed through `BatchEvents`; the command renders the events.
"""

import contextlib
from typing import TYPE_CHECKING, Protocol, cast

from ocfl_cli.errors import (
    AttestationMismatchError,
    CommitFailedError,
    ErrorContext,
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
    batch_command_tuple,
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
from ocfl_lib import BitcoinRpcError

if TYPE_CHECKING:
    from collections.abc import Callable

    from ocfl_cli.config import Config
    from ocfl_cli.ports import BtcRpcFactory, HashBlockStream, SpClientFactory
    from ocfl_cli.store.staging import StagingStore
    from ocfl_lib import StorageRoot
    from ocfl_lib.fs import WriteFS


class BatchEvents(Protocol):
    """What a batch run reports, in order, as it goes."""

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


def _resumed_batch(
    staging_root, batch_id: str, target: batch_model.BatchTarget
) -> batch_model.BatchFile:
    batch = load_batch_or_usage(staging_root, batch_id)
    if batch.target != target:
        raise UsageError(
            f"batch {batch.batch_id} target is {batch.target!r}, not {target!r}"
        )
    return batch


def _new_or_resumed(
    store: StagingStore,
    *,
    target: batch_model.BatchTarget,
    root_path: str,
    selected: list[str],
    batch_id: str | None,
    category: int | None,
    default_category: int,
) -> tuple[batch_model.BatchFile, bool]:
    if batch_id is not None and (selected or category is not None):
        raise UsageError(
            "--batch is mutually exclusive with --object, --objects-file, "
            "and --category"
        )
    if batch_id is None and not selected:
        raise UsageError("new batch requires --object or --objects-file")
    if batch_id is not None:
        return _resumed_batch(store.root, batch_id, target), False
    now = batch_state.now_utc_iso_z()
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
    target: batch_model.BatchTarget,
    root_path: str,
    batch: batch_model.BatchFile,
    created: bool,
    selected: list[str],
    allow_missing_metadata: bool,
    sp_factory: SpClientFactory,
    events: BatchEvents,
) -> batch_model.BatchFile:
    """Create (or resume) the batch, recover lost intents, submit what is due."""
    staging_root = store.root
    if created:
        with batch_state.with_batch_lock(staging_root, batch.batch_id):
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
            )
            batch = replace_batch_object(batch, row)
            with batch_state.with_batch_lock(staging_root, batch.batch_id):
                batch_state.save_batch(staging_root, batch)
            events.object_prepared(batch, row)
    else:
        events.resumed(batch)
    recovered = collect_recoverable_object_intents(store, batch)
    with batch_state.with_batch_lock(staging_root, batch.batch_id):
        before_recovered = batch
        batch = batch_state.load_batch(staging_root, batch.batch_id)
        batch = merge_recovered_object_intents(batch, recovered)
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
    )
    events.changed(before_submit, batch)
    return batch


def submit(
    store: StagingStore,
    cfg: Config,
    sr: StorageRoot,
    *,
    target: batch_model.BatchTarget,
    root_path: str,
    selected: list[str],
    batch_id: str | None,
    category: int | None,
    allow_missing_metadata: bool,
    sp_factory: SpClientFactory,
    events: BatchEvents,
) -> batch_model.BatchFile:
    """`stage timestamp batch content|inventory`: create or resume, then submit."""
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
    )
    with batch_state.with_driver_lease(
        store.root,
        batch.batch_id,
        command=batch_command_tuple(target, batch.batch_id),
    ):
        return _start(
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
        )


def retry(
    store: StagingStore,
    cfg: Config,
    *,
    target: batch_model.BatchTarget,
    batch_id: str,
    sp_factory: SpClientFactory,
    events: BatchEvents,
) -> batch_model.BatchFile:
    """`--retry --batch ID`: resubmit the batch's failed chunks."""
    staging_root = store.root
    batch = _resumed_batch(staging_root, batch_id, target)
    with batch_state.with_driver_lease(
        staging_root,
        batch.batch_id,
        command=(*batch_command_tuple(target, batch.batch_id), "--retry"),
    ):
        events.resumed(batch)
        before_retry = batch
        batch = retry_batch_submissions(
            target=target,
            cfg=cfg,
            store=store,
            staging_root=staging_root,
            batch=batch,
            sp_factory=sp_factory,
        )
        events.changed(before_retry, batch)
    return batch


def upgrade(
    store: StagingStore,
    cfg: Config,
    open_root: Callable[[], StorageRoot],
    *,
    target: batch_model.BatchTarget,
    batch_id: str,
    sp_factory: SpClientFactory,
    btc_factory: BtcRpcFactory,
    events: BatchEvents,
) -> batch_model.BatchFile:
    """`--upgrade --batch ID`: one upgrade pass over the batch."""
    staging_root = store.root
    batch = _resumed_batch(staging_root, batch_id, target)
    sr = open_root()
    with batch_state.with_driver_lease(
        staging_root,
        batch.batch_id,
        command=(*batch_command_tuple(target, batch.batch_id), "--upgrade"),
    ):
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
        )
        events.changed(before_upgrade, batch)
    return batch


def watch(
    store: StagingStore,
    cfg: Config,
    sr: StorageRoot,
    *,
    target: batch_model.BatchTarget,
    root_path: str,
    selected: list[str],
    batch_id: str | None,
    category: int | None,
    allow_missing_metadata: bool,
    sp_factory: SpClientFactory,
    btc_factory: BtcRpcFactory,
    events: BatchEvents,
) -> int:
    """`--watch`: submit, then run an upgrade pass per Bitcoin block.

    Returns the exit code: 0 when every row upgraded, 1 when the batch
    finished with failures, 130 when interrupted.
    """
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
    )
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
        )
        events.changed(before_upgrade, batch)
        if batch_has_submitted_subset(batch):
            return batch, False
        exit_code = 0 if batch_watch_successful(batch) else 1
        return batch, True

    try:
        with batch_state.with_driver_lease(
            staging_root,
            batch.batch_id,
            command=(*batch_command_tuple(target, batch.batch_id), "--watch"),
        ):
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
            )
            batch, done = run_watch_upgrade_pass(batch)
            if done:
                return exit_code

            events.waiting(batch)
            btc = btc_factory(cfg)
            stream = wf_watch.block_stream_for(cfg)
            stream_cm = cast(
                "contextlib.AbstractContextManager[HashBlockStream]",
                stream
                if hasattr(stream, "__enter__")
                else contextlib.nullcontext(stream),
            )
            last_height: int | None = None
            last_tip_hash: str | None = None
            with stream_cm as zmq_sub:
                while True:
                    block = zmq_sub.wait_for_block(timeout_ms=5000)
                    if block is None:
                        batch, done = run_watch_upgrade_pass(batch, reload_batch=True)
                        if done:
                            return exit_code
                        continue
                    wf_watch.sleep_after_block()
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
    if isinstance(exc, PhaseError):
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
) -> int:
    """`stage commit --batch ID`: commit every upgraded object; returns failures."""
    staging_root = store.root
    with batch_state.with_batch_lock(staging_root, batch_id):
        batch = load_batch_or_usage(staging_root, batch_id)
        if batch.target != "inventory":
            raise UsageError(
                f"commit batches must target inventory; got {batch.target}"
            )
        if batch.root_path != root_path:
            raise UsageError(
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
            wf_commit.commit_ots(
                store=store,
                sr=sr,
                fs=fs,
                root_path=batch.root_path,
                object_id=object_id,
                staging_dir=staging_dir,
                allow_missing_metadata=False,
            )
        except (
            AttestationMismatchError,
            PhaseError,
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
