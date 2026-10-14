import json
from pathlib import Path  # noqa: TC003 - Typer reads option annotations at runtime.
from typing import TYPE_CHECKING, Annotated

import typer

from ocfl_cli._common import open_root, output_json
from ocfl_cli.errors import ObjectsNotStagedError, UsageError
from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.model.phase import TimestampTarget
from ocfl_cli.stage._app import (
    StageCtx,
    require_bitcoin,
    require_simpleproof,
    resolve_root_path,
    stage_of,
)
from ocfl_cli.stage.render import render_section, short_job_id
from ocfl_cli.workflow.batch import drive
from ocfl_cli.workflow.batch.prepare import read_objects_file
from ocfl_cli.workflow.batch.report import (
    BatchCreatedEventOutput,
    BatchEventOutput,
    BatchResumedEventOutput,
    BatchSummaryEventOutput,
    CommitObjectEventOutput,
    CommitSummaryEventOutput,
    batch_counts,
    batch_object_event,
    batch_status_dict,
    batch_to_list_row,
    batch_verified_digest_counts,
    batch_verify_job_status_counts,
    load_batch_or_usage,
    load_batches_or_exit,
    not_staged,
    remove_batch,
    upgraded_objects,
)

if TYPE_CHECKING:
    from ocfl_cli.model.batch import BatchFile, BatchObject


def _batch_verify_job_transitions(
    before: BatchFile, after: BatchFile
) -> tuple[str, ...]:
    prior = {job.job_id: job for job in before.verify_jobs}
    transitions: list[str] = []
    for job in after.verify_jobs:
        old = prior.get(job.job_id)
        if old is None:
            transitions.append(f"job {short_job_id(job.job_id)}: new {job.status}")
        elif old.status != job.status:
            transitions.append(
                f"job {short_job_id(job.job_id)}: {old.status} -> {job.status}"
            )
    return tuple(transitions)


def short_block_hash(block_hash: str) -> str:
    if len(block_hash) <= 12:
        return block_hash
    return block_hash[-12:]


class _CliBatchEvents:
    def __init__(self, stage: StageCtx, *, resumed_by_id: bool) -> None:
        self._json = stage.json
        self._staging_root = stage.store.root
        self._resumed_by_id = resumed_by_id
        # The batch as the last watch left it.
        self.final: BatchFile | None = None

    def _emit(self, data: BatchEventOutput) -> None:
        if self._json:
            typer.echo(json.dumps(data, separators=(",", ":")))

    def created(self, batch: BatchFile) -> None:
        data: BatchCreatedEventOutput = {
            "event": "batch_created",
            "batch_id": batch.batch_id,
            "target": batch.target,
            "category_id": batch.category_id,
            "root_path": batch.root_path,
            "created_at": batch.created_at,
        }
        if self._json:
            self._emit(data)
        else:
            typer.echo(
                f"batch created: {batch.batch_id} target={batch.target} "
                f"category={batch.category_id}"
            )

    def resumed(self, batch: BatchFile) -> None:
        if self._json:
            data: BatchResumedEventOutput = {
                "event": "batch_resumed",
                "batch_id": batch.batch_id,
                "target": batch.target,
                "category_id": batch.category_id,
                "root_path": batch.root_path,
            }
            self._emit(data)
        else:
            typer.echo(f"batch resumed: {batch.batch_id} target={batch.target}")

    def object_prepared(self, batch: BatchFile, row: BatchObject) -> None:
        self._emit(batch_object_event(self._staging_root, batch, row))

    def changed(self, before: BatchFile, after: BatchFile) -> None:
        previous = {row.object_id: row for row in before.objects}
        for row in after.objects:
            old = previous.get(row.object_id)
            if old is None:
                self.object_prepared(after, row)
                continue
            old_event = batch_object_event(self._staging_root, before, old)
            new_event = batch_object_event(self._staging_root, after, row)
            if old_event != new_event:
                self._emit(new_event)

    def summary(self, batch: BatchFile) -> None:
        event: BatchSummaryEventOutput = {
            "event": "summary",
            "batch_id": batch.batch_id,
            "target": batch.target,
            "category_id": batch.category_id,
            "root_path": batch.root_path,
            "counts": batch_counts(batch),
        }
        self._emit(event)

    def waiting(self, batch: BatchFile) -> None:
        if self._json:
            return
        counts = batch_counts(batch)
        typer.echo(
            f"batch submitted: {batch.batch_id} submitted={counts['submitted']} "
            f"upgraded={counts['upgraded']} failed={counts['failed']}; "
            "waiting for Bitcoin blocks"
        )

    def block(
        self,
        before: BatchFile,
        after: BatchFile,
        *,
        height: int,
        block_hash: str,
        reverified: bool,
    ) -> None:
        if self._json:
            return
        counts = batch_counts(after)
        verified, total = batch_verified_digest_counts(self._staging_root, after)
        jobs = batch_verify_job_status_counts(after)
        reverify_text = " reverified" if reverified else ""
        typer.echo(
            f"block {height} {short_block_hash(block_hash)}: "
            f"submitted={counts['submitted']} "
            f"upgraded={counts['upgraded']} failed={counts['failed']} "
            f"verified={verified}/{total} jobs={jobs}{reverify_text}"
        )
        for transition in _batch_verify_job_transitions(before, after):
            typer.echo(f"  {transition}")

    def watch_done(self, batch: BatchFile) -> None:
        self.final = batch
        if self._json:
            self.summary(batch)
        elif self._resumed_by_id:
            counts = batch_counts(batch)
            typer.echo(
                f"batch watched: {batch.batch_id} submitted={counts['submitted']} "
                f"upgraded={counts['upgraded']} failed={counts['failed']}"
            )

    def commit_event(
        self, data: CommitObjectEventOutput | CommitSummaryEventOutput
    ) -> None:
        if self._json:
            typer.echo(json.dumps(data, separators=(",", ":")))
            return
        if data["event"] == "summary":
            counts = data["counts"]
            typer.echo(
                f"batch committed: {data['batch_id']} "
                f"committed={counts['committed']} "
                f"skipped={counts['skipped']} failed={counts['failed']} "
                f"total={counts['total']}"
            )
            return
        object_id = data["object_id"]
        if data["event"] == "object_committed":
            typer.echo(f"{object_id}: committed")
        elif data["event"] == "object_skipped":
            typer.echo(f"{object_id}: skipped ({data['reason']})")
        else:
            typer.echo(f"{object_id}: failed ({data['reason']}): {data['message']}")


BatchArg = Annotated[str, typer.Argument(help="Batch ID.")]


def _selected(object_ids: list[str] | None, objects_file: Path | None) -> list[str]:
    selected = list(object_ids or ())
    if objects_file is not None:
        selected.extend(read_objects_file(objects_file))
    return selected


def _require_staged(batch: BatchFile | None) -> None:
    if batch is None:
        return
    if missing := not_staged(batch):
        raise ObjectsNotStagedError(
            f"no OTS staging for {', '.join(missing)} under {batch.root_path} "
            f"(batch {batch.batch_id})"
        )


def _report_submitted(stage: StageCtx, batch: BatchFile) -> None:
    if not stage.json:
        counts = batch_counts(batch)
        typer.echo(
            f"batch submitted: {batch.batch_id} prepared={counts['prepared']} "
            f"submitted={counts['submitted']} skipped={counts['skipped']} "
            f"failed={counts['failed']}"
        )


def _watch(
    ctx: typer.Context,
    *,
    target: TimestampTarget | None,
    selected: list[str],
    batch_id: str | None,
    category: int | None,
    allow_missing_metadata: bool,
) -> None:
    stage = stage_of(ctx)
    cfg = stage.config
    require_simpleproof(ctx, cfg)
    require_bitcoin(ctx, cfg)
    sr, _fs = open_root(stage)
    events = _CliBatchEvents(stage, resumed_by_id=batch_id is not None)
    exit_code = drive.watch(
        stage.store,
        cfg,
        sr,
        target=target,
        root_path=resolve_root_path(stage.root),
        selected=selected,
        batch_id=batch_id,
        category=category,
        allow_missing_metadata=allow_missing_metadata,
        sp_factory=stage.sp_client_factory,
        btc_factory=stage.btc_rpc_factory,
        events=events,
        runtime=stage.runtime,
    )
    if exit_code:
        raise typer.Exit(exit_code)
    _require_staged(events.final)


def _submit(
    ctx: typer.Context,
    *,
    target: TimestampTarget | None,
    selected: list[str],
    batch_id: str | None,
    category: int | None,
    allow_missing_metadata: bool,
) -> None:
    stage = stage_of(ctx)
    cfg = stage.config
    require_simpleproof(ctx, cfg)
    sr, _fs = open_root(stage)
    events = _CliBatchEvents(stage, resumed_by_id=batch_id is not None)
    batch = drive.submit(
        stage.store,
        cfg,
        sr,
        target=target,
        root_path=resolve_root_path(stage.root),
        selected=selected,
        batch_id=batch_id,
        category=category,
        allow_missing_metadata=allow_missing_metadata,
        sp_factory=stage.sp_client_factory,
        events=events,
        runtime=stage.runtime,
    )
    events.summary(batch)
    _report_submitted(stage, batch)
    _require_staged(batch)


def batch_create(
    ctx: typer.Context,
    target: Annotated[
        TimestampTarget, typer.Argument(help="What the batch timestamps.")
    ],
    object_ids: Annotated[
        list[str] | None, typer.Argument(help="Object IDs to put in the batch.")
    ] = None,
    objects_file: Annotated[
        Path | None,
        typer.Option("--objects-file", help="File of object IDs, one per line."),
    ] = None,
    category: Annotated[
        int | None,
        typer.Option("--category", help="SimpleProof category for the batch."),
    ] = None,
    from_batch: Annotated[
        str | None,
        typer.Option(
            "--from",
            help="Inventory batches: the objects a content batch upgraded.",
        ),
    ] = None,
    allow_missing_metadata: Annotated[
        bool,
        typer.Option(
            "--allow-missing-metadata",
            help="Inventory batches: timestamp even when staged metadata is "
            "incomplete.",
        ),
    ] = False,
    watch_flag: Annotated[
        bool,
        typer.Option(
            "--watch",
            help="Then upgrade on each Bitcoin block until the proofs are final.",
        ),
    ] = False,
) -> None:
    """Create a batch of Staging Areas and submit their digests together."""
    stage = stage_of(ctx)
    if target is TimestampTarget.CONTENT:
        for flag, given in (
            ("--allow-missing-metadata", allow_missing_metadata),
            ("--from", from_batch is not None),
        ):
            if given:
                raise UsageError(f"{flag} applies to inventory batches only")
    selected = _selected(object_ids, objects_file)
    if from_batch is not None:
        selected.extend(upgraded_objects(stage.store.root, from_batch))
    if not selected:
        raise UsageError("a new batch needs OBJECT..., --objects-file or --from")
    run = _watch if watch_flag else _submit
    run(
        ctx,
        target=target,
        selected=selected,
        batch_id=None,
        category=category,
        allow_missing_metadata=allow_missing_metadata,
    )


def batch_submit(ctx: typer.Context, batch_id: BatchArg) -> None:
    """Submit what a batch still has to send (resume after a failure)."""
    _submit(
        ctx,
        target=None,
        selected=[],
        batch_id=batch_id,
        category=None,
        allow_missing_metadata=False,
    )


def batch_upgrade(
    ctx: typer.Context,
    batch_id: BatchArg,
    watch_flag: Annotated[
        bool,
        typer.Option(
            "--watch",
            help="Upgrade on each Bitcoin block until the proofs are final.",
        ),
    ] = False,
) -> None:
    """Fetch the batch's proofs from SimpleProof and verify them against Bitcoin."""
    stage = stage_of(ctx)
    if watch_flag:
        _watch(
            ctx,
            target=None,
            selected=[],
            batch_id=batch_id,
            category=None,
            allow_missing_metadata=False,
        )
        return
    cfg = stage.config
    require_simpleproof(ctx, cfg)
    require_bitcoin(ctx, cfg)
    events = _CliBatchEvents(stage, resumed_by_id=True)
    batch = drive.upgrade(
        stage.store,
        cfg,
        lambda: open_root(stage)[0],
        batch_id=batch_id,
        sp_factory=stage.sp_client_factory,
        btc_factory=stage.btc_rpc_factory,
        events=events,
        runtime=stage.runtime,
    )
    events.summary(batch)
    if not stage.json:
        counts = batch_counts(batch)
        typer.echo(
            f"batch upgraded: {batch.batch_id} submitted={counts['submitted']} "
            f"upgraded={counts['upgraded']} failed={counts['failed']}"
        )
    if batch_counts(batch)["submitted"]:
        raise typer.Exit(ExitCode.PENDING)


def batch_retry(ctx: typer.Context, batch_id: BatchArg) -> None:
    """Resubmit the batch's failed SimpleProof submissions."""
    stage = stage_of(ctx)
    cfg = stage.config
    require_simpleproof(ctx, cfg)
    events = _CliBatchEvents(stage, resumed_by_id=True)
    batch = drive.retry(
        stage.store,
        cfg,
        batch_id=batch_id,
        sp_factory=stage.sp_client_factory,
        events=events,
        runtime=stage.runtime,
    )
    events.summary(batch)
    if not stage.json:
        counts = batch_counts(batch)
        typer.echo(
            f"batch retried: {batch.batch_id} prepared={counts['prepared']} "
            f"submitted={counts['submitted']} failed={counts['failed']}"
        )


def batch_commit(ctx: typer.Context, batch_id: BatchArg) -> None:
    """Commit every object of an inventory batch whose proofs are final."""
    stage = stage_of(ctx)
    failures = drive.commit(
        stage.store,
        lambda: open_root(stage),
        root_path=resolve_root_path(stage.root),
        batch_id=batch_id,
        events=_CliBatchEvents(stage, resumed_by_id=True),
        runtime=stage.runtime,
    )
    if failures:
        raise typer.Exit(1)


def batch_ls(ctx: typer.Context) -> None:
    """List the batches in the staging root."""
    stage = stage_of(ctx)
    staging_root = stage.store.root
    rows = [batch_to_list_row(batch) for batch in load_batches_or_exit(staging_root)]
    if stage.json:
        output_json(rows)
        return
    for row in rows:
        typer.echo(
            f"{row['short_batch_id']}  {row['target']}  "
            f"created={row['created_at']} updated={row['updated_at']}  "
            f"prepared={row['prepared']} submitted={row['submitted']} "
            f"upgraded={row['upgraded']} failed={row['failed']} "
            f"skipped={row['skipped']} total={row['total']}"
        )


def batch_status(
    ctx: typer.Context,
    batch_id: BatchArg,
    detail: Annotated[
        bool,
        typer.Option("--detail", help="Show per-digest submit/verify state."),
    ] = False,
) -> None:
    """Show each object of a batch: its status and the next command."""
    stage = stage_of(ctx)
    out = stage.console
    staging_root = stage.store.root
    batch = load_batch_or_usage(staging_root, batch_id)
    data = batch_status_dict(staging_root, batch, verbose=detail)
    if stage.json:
        output_json(data)
        return
    render_section(out, "Batch")
    typer.echo(f"  ID: {batch.batch_id}")
    typer.echo(f"  Target: {batch.target}")
    typer.echo(f"  Category: {batch.category_id}")
    typer.echo(f"  Updated: {batch.updated_at}")
    render_section(out, "Objects", leading_blank=True)
    for row in data["objects"]:
        typer.echo(
            f"  {row['object_id']}  {row['batch_status']}  "
            f"{row['digests_verified']}/{row['digests_total']} verified  "
            f"Next: {row['next']}"
        )
        if detail:
            for digest in row["digests"]:
                typer.echo(
                    f"    {digest['digest']}  submitted={digest['submitted']} "
                    f"verified={digest['verified']} "
                    f"submit={digest['submit_status']} "
                    f"verify={digest['verify_status']}"
                )


def batch_rm(
    ctx: typer.Context,
    batch_id: BatchArg,
    force: Annotated[
        bool,
        typer.Option("--force", help="Delete the batch even with open rows."),
    ] = False,
) -> None:
    """Delete a batch's file when cleanup is safe."""
    stage = stage_of(ctx)
    removed = remove_batch(
        stage.store.root, batch_id, force=force, runtime=stage.runtime
    )
    typer.echo(f"removed batch {removed}")
