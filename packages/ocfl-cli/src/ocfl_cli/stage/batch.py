"""stage timestamp batch content|inventory|ls|status|rm, and stage commit --batch.

The batch engine is `ocfl_cli.workflow.batch`; this module parses options and
renders the events it reports (JSON lines under `--json`, text otherwise).
"""

import json
from pathlib import Path  # noqa: TC003 - Typer reads option annotations at runtime.
from typing import TYPE_CHECKING, Annotated, cast

import typer

from ocfl_cli import _appctx as appctx
from ocfl_cli._common import open_root, output_json
from ocfl_cli.errors import UsageError
from ocfl_cli.stage._app import (
    require_bitcoin,
    require_config,
    require_simpleproof,
    resolve_root_path,
    stage_store,
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
    remove_batch,
)

if TYPE_CHECKING:
    from ocfl_cli.model.batch import BatchFile, BatchObject, BatchTarget


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


def _short_block_hash(block_hash: str) -> str:
    if len(block_hash) <= 12:
        return block_hash
    return block_hash[-12:]


class _CliBatchEvents:
    """Renders batch events: JSON lines under `--json`, text lines otherwise."""

    def __init__(self, ctx: typer.Context, *, resumed_by_id: bool) -> None:
        self._json = appctx.json_mode(ctx)
        self._staging_root = stage_store(ctx).root
        self._resumed_by_id = resumed_by_id

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
            f"block {height} {_short_block_hash(block_hash)}: "
            f"submitted={counts['submitted']} "
            f"upgraded={counts['upgraded']} failed={counts['failed']} "
            f"verified={verified}/{total} jobs={jobs}{reverify_text}"
        )
        for transition in _batch_verify_job_transitions(before, after):
            typer.echo(f"  {transition}")

    def watch_done(self, batch: BatchFile) -> None:
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
            summary = cast("CommitSummaryEventOutput", data)
            counts = summary["counts"]
            typer.echo(
                f"batch committed: {summary['batch_id']} "
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


def commit_batch(ctx: typer.Context, batch_id: str) -> None:
    """`stage commit --batch ID`."""
    failures = drive.commit(
        stage_store(ctx),
        lambda: open_root(ctx),
        root_path=resolve_root_path(ctx),
        batch_id=batch_id,
        events=_CliBatchEvents(ctx, resumed_by_id=True),
        runtime=appctx.runtime(ctx),
    )
    if failures:
        raise typer.Exit(1)


def _selected(object_ids: tuple[str, ...], objects_file: Path | None) -> list[str]:
    selected = list(object_ids)
    if objects_file is not None:
        selected.extend(read_objects_file(objects_file))
    return selected


def _drive_batch_retry(
    ctx: typer.Context, *, target: BatchTarget, batch_id: str | None
) -> None:
    if batch_id is None:
        raise UsageError("--retry requires --batch")
    cfg = require_config(ctx)
    require_simpleproof(ctx, cfg)
    events = _CliBatchEvents(ctx, resumed_by_id=True)
    batch = drive.retry(
        stage_store(ctx),
        cfg,
        target=target,
        batch_id=batch_id,
        sp_factory=appctx.sp_client_factory(ctx),
        events=events,
        runtime=appctx.runtime(ctx),
    )
    events.summary(batch)
    if not appctx.json_mode(ctx):
        counts = batch_counts(batch)
        typer.echo(
            f"batch retried: {batch.batch_id} prepared={counts['prepared']} "
            f"submitted={counts['submitted']} failed={counts['failed']}"
        )


def _drive_batch_upgrade(
    ctx: typer.Context, *, target: BatchTarget, batch_id: str | None
) -> None:
    if batch_id is None:
        raise UsageError("--upgrade requires --batch")
    cfg = require_config(ctx)
    require_simpleproof(ctx, cfg)
    require_bitcoin(ctx, cfg)
    events = _CliBatchEvents(ctx, resumed_by_id=True)
    batch = drive.upgrade(
        stage_store(ctx),
        cfg,
        lambda: open_root(ctx)[0],
        target=target,
        batch_id=batch_id,
        sp_factory=appctx.sp_client_factory(ctx),
        btc_factory=appctx.btc_rpc_factory(ctx),
        events=events,
        runtime=appctx.runtime(ctx),
    )
    events.summary(batch)
    if not appctx.json_mode(ctx):
        counts = batch_counts(batch)
        typer.echo(
            f"batch upgraded: {batch.batch_id} submitted={counts['submitted']} "
            f"upgraded={counts['upgraded']} failed={counts['failed']}"
        )


def _drive_batch_watch(
    ctx: typer.Context,
    *,
    target: BatchTarget,
    object_ids: tuple[str, ...],
    objects_file: Path | None,
    batch_id: str | None,
    category: int | None,
    allow_missing_metadata: bool,
) -> None:
    cfg = require_config(ctx)
    require_simpleproof(ctx, cfg)
    require_bitcoin(ctx, cfg)
    store = stage_store(ctx)
    root_path = resolve_root_path(ctx)
    sr, _fs = open_root(ctx)
    exit_code = drive.watch(
        store,
        cfg,
        sr,
        target=target,
        root_path=root_path,
        selected=_selected(object_ids, objects_file),
        batch_id=batch_id,
        category=category,
        allow_missing_metadata=allow_missing_metadata,
        sp_factory=appctx.sp_client_factory(ctx),
        btc_factory=appctx.btc_rpc_factory(ctx),
        events=_CliBatchEvents(ctx, resumed_by_id=batch_id is not None),
        runtime=appctx.runtime(ctx),
    )
    if exit_code:
        raise typer.Exit(exit_code)


def _drive_batch_submit(
    ctx: typer.Context,
    *,
    target: BatchTarget,
    object_ids: tuple[str, ...],
    objects_file: Path | None,
    batch_id: str | None,
    category: int | None,
    allow_missing_metadata: bool,
) -> None:
    cfg = require_config(ctx)
    require_simpleproof(ctx, cfg)
    store = stage_store(ctx)
    root_path = resolve_root_path(ctx)
    sr, _fs = open_root(ctx)
    events = _CliBatchEvents(ctx, resumed_by_id=batch_id is not None)
    batch = drive.submit(
        store,
        cfg,
        sr,
        target=target,
        root_path=root_path,
        selected=_selected(object_ids, objects_file),
        batch_id=batch_id,
        category=category,
        allow_missing_metadata=allow_missing_metadata,
        sp_factory=appctx.sp_client_factory(ctx),
        events=events,
        runtime=appctx.runtime(ctx),
    )
    events.summary(batch)
    if not appctx.json_mode(ctx):
        counts = batch_counts(batch)
        typer.echo(
            f"batch submitted: {batch.batch_id} prepared={counts['prepared']} "
            f"submitted={counts['submitted']} skipped={counts['skipped']} "
            f"failed={counts['failed']}"
        )


def timestamp_batch_content(
    ctx: typer.Context,
    object_ids: Annotated[
        list[str] | None,
        typer.Option("--object", help="Object ID to include in a new batch."),
    ] = None,
    objects_file: Annotated[
        Path | None,
        typer.Option("--objects-file", help="File containing object IDs."),
    ] = None,
    batch_id: Annotated[
        str | None,
        typer.Option("--batch", help="Resume an existing batch."),
    ] = None,
    category: Annotated[
        int | None,
        typer.Option("--category", help="SimpleProof category for a new batch."),
    ] = None,
    upgrade: Annotated[
        bool,
        typer.Option("--upgrade", help="Poll SP verify jobs and ingest proofs."),
    ] = False,
    retry: Annotated[
        bool,
        typer.Option("--retry", help="Repair and resubmit failed batch chunks."),
    ] = False,
    watch_flag: Annotated[
        bool,
        typer.Option(
            "--watch",
            help="ZMQ-driven loop; runs a batch upgrade pass per Bitcoin block.",
        ),
    ] = False,
) -> None:
    """Create or resume a content timestamping batch."""
    if retry and (upgrade or watch_flag):
        raise UsageError("--retry is mutually exclusive with --upgrade and --watch")
    if upgrade and watch_flag:
        raise UsageError("--upgrade is mutually exclusive with --watch")
    if retry:
        if object_ids or objects_file is not None or category is not None:
            raise UsageError("--retry is only supported with --batch")
        _drive_batch_retry(ctx, target="content", batch_id=batch_id)
        return
    if watch_flag:
        _drive_batch_watch(
            ctx,
            target="content",
            object_ids=tuple(object_ids or ()),
            objects_file=objects_file,
            batch_id=batch_id,
            category=category,
            allow_missing_metadata=False,
        )
        return
    if upgrade:
        if object_ids or objects_file is not None or category is not None:
            raise UsageError("--upgrade is only supported with --batch")
        _drive_batch_upgrade(ctx, target="content", batch_id=batch_id)
        return
    _drive_batch_submit(
        ctx,
        target="content",
        object_ids=tuple(object_ids or ()),
        objects_file=objects_file,
        batch_id=batch_id,
        category=category,
        allow_missing_metadata=False,
    )


def timestamp_batch_inventory(
    ctx: typer.Context,
    object_ids: Annotated[
        list[str] | None,
        typer.Option("--object", help="Object ID to include in a new batch."),
    ] = None,
    objects_file: Annotated[
        Path | None,
        typer.Option("--objects-file", help="File containing object IDs."),
    ] = None,
    batch_id: Annotated[
        str | None,
        typer.Option("--batch", help="Resume an existing batch."),
    ] = None,
    category: Annotated[
        int | None,
        typer.Option("--category", help="SimpleProof category for a new batch."),
    ] = None,
    allow_missing_metadata: Annotated[
        bool,
        typer.Option(
            "--allow-missing-metadata",
            help="Timestamp inventory even when staged metadata is incomplete.",
        ),
    ] = False,
    upgrade: Annotated[
        bool,
        typer.Option("--upgrade", help="Poll SP verify jobs and ingest proofs."),
    ] = False,
    retry: Annotated[
        bool,
        typer.Option("--retry", help="Repair and resubmit failed batch chunks."),
    ] = False,
    watch_flag: Annotated[
        bool,
        typer.Option(
            "--watch",
            help="ZMQ-driven loop; runs a batch upgrade pass per Bitcoin block.",
        ),
    ] = False,
) -> None:
    """Create or resume an inventory timestamping batch."""
    if retry and (upgrade or watch_flag):
        raise UsageError("--retry is mutually exclusive with --upgrade and --watch")
    if upgrade and watch_flag:
        raise UsageError("--upgrade is mutually exclusive with --watch")
    if retry:
        if (
            object_ids
            or objects_file is not None
            or category is not None
            or allow_missing_metadata
        ):
            raise UsageError("--retry is only supported with --batch")
        _drive_batch_retry(ctx, target="inventory", batch_id=batch_id)
        return
    if watch_flag:
        _drive_batch_watch(
            ctx,
            target="inventory",
            object_ids=tuple(object_ids or ()),
            objects_file=objects_file,
            batch_id=batch_id,
            category=category,
            allow_missing_metadata=allow_missing_metadata,
        )
        return
    if upgrade:
        if object_ids or objects_file is not None or category is not None:
            raise UsageError("--upgrade is only supported with --batch")
        _drive_batch_upgrade(ctx, target="inventory", batch_id=batch_id)
        return
    _drive_batch_submit(
        ctx,
        target="inventory",
        object_ids=tuple(object_ids or ()),
        objects_file=objects_file,
        batch_id=batch_id,
        category=category,
        allow_missing_metadata=allow_missing_metadata,
    )


def timestamp_batch_ls(ctx: typer.Context) -> None:
    """List local batch timestamping state files."""
    staging_root = stage_store(ctx).root
    rows = [batch_to_list_row(batch) for batch in load_batches_or_exit(staging_root)]
    if appctx.json_mode(ctx):
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


def timestamp_batch_status(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Argument(help="Batch ID.")],
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="Show per-digest submit/verify state."),
    ] = False,
) -> None:
    """Show local per-object status for a batch."""
    out = appctx.console(ctx)
    staging_root = stage_store(ctx).root
    batch = load_batch_or_usage(staging_root, batch_id)
    data = batch_status_dict(staging_root, batch, verbose=verbose)
    if appctx.json_mode(ctx):
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
        if verbose:
            for digest in row["digests"]:
                typer.echo(
                    f"    {digest['digest']}  submitted={digest['submitted']} "
                    f"verified={digest['verified']} "
                    f"submit={digest['submit_status']} "
                    f"verify={digest['verify_status']}"
                )


def timestamp_batch_rm(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Argument(help="Batch ID.")],
    force: Annotated[
        bool,
        typer.Option("--force", help="Delete the batch file even with open rows."),
    ] = False,
) -> None:
    """Delete a local batch file when cleanup is safe."""
    remove_batch(stage_store(ctx).root, batch_id, force=force)
    typer.echo(f"removed batch {batch_id}")
