"""stage timestamp content and inventory."""

from typing import TYPE_CHECKING, Annotated, NoReturn

import typer

from ocfl_cli import _appctx as appctx
from ocfl_cli import errors as errors
from ocfl_cli._common import open_root
from ocfl_cli.errors import (
    AttestationMismatchError,
    ErrorContext,
    PhaseError,
    ReorgDetectedError,
)
from ocfl_cli.model.phase import Phase, TimestampTarget
from ocfl_cli.stage._app import (
    TimestampPreflight,
    require_timestamp_preflight,
    validate_inventory_metadata_flag,
)
from ocfl_cli.stage.render import pluralize, render_section, short_job_id
from ocfl_cli.workflow import common as wf_common
from ocfl_cli.workflow import staging as wf_staging
from ocfl_cli.workflow import status as wf_status
from ocfl_cli.workflow import timestamp, watch
from ocfl_cli.workflow.targets import rules_for

if TYPE_CHECKING:
    from collections.abc import Callable

    from rich.console import Console


def render_watch_start(
    out: Console,
    object_id: str,
    jobs: tuple[wf_common.JobProgress, ...] = (),
    *,
    waiting: bool = True,
) -> None:
    render_section(out, "Watch")
    typer.echo(f"  Object: {object_id}")
    _render_sp_submit_warning(jobs)
    if waiting:
        typer.echo("  Waiting for Bitcoin blocks")


def _job_by_id(
    jobs: tuple[wf_common.JobProgress, ...],
) -> dict[str, wf_common.JobProgress]:
    return {job.job_id: job for job in jobs}


def _render_calendar_counts(
    counts: tuple[tuple[str, int], ...],
    *,
    prior: tuple[tuple[str, int], ...] | None = None,
) -> None:
    prior_counts = dict(prior or ())
    for calendar, count in counts:
        delta = count - prior_counts.get(calendar, 0)
        suffix = f" (+{delta})" if prior is not None and delta > 0 else ""
        typer.echo(f"        {calendar}: {count}{suffix}")


def _render_jobs(
    jobs: tuple[wf_common.JobProgress, ...],
    *,
    prior: tuple[wf_common.JobProgress, ...] | None = None,
) -> None:
    if not jobs:
        return
    typer.echo("  Jobs:")
    prior_jobs = _job_by_id(prior or ())
    for job in jobs:
        prior_job = prior_jobs.get(job.job_id)
        if job.total_hashes and job.verified_hashes is not None:
            summary = f"{job.verified_hashes}/{job.total_hashes} verified"
            if prior is not None:
                prior_verified = prior_job.verified_hashes if prior_job else None
                delta = (
                    job.verified_hashes - prior_verified
                    if prior_verified is not None
                    else 0
                )
                summary += f" (+{delta} since last block)" if delta else " (no change)"
            elif job.total_hashes:
                pct = round((job.verified_hashes / job.total_hashes) * 100)
                summary += f" ({pct}%)"
        else:
            summary = job.status
        age = (
            f", age {wf_status.humanize_age(job.age_seconds)}"
            if job.age_seconds is not None and prior is None
            else ""
        )
        typer.echo(f"    job {short_job_id(job.job_id)} {job.status} — {summary}{age}")
        prior_anchored = prior_job.anchored_digests if prior_job is not None else 0
        if prior is not None:
            delta = job.anchored_digests - prior_anchored
            anchored = (
                f"+{delta} digests" if delta > 0 else f"{job.anchored_digests} digests"
            )
        else:
            anchored = f"{job.anchored_digests} digests"
        typer.echo(f"      anchored: {anchored}")
        prior_counts = None
        if prior is not None and prior_job is not None:
            prior_counts = prior_job.calendar_counts
        _render_calendar_counts(
            job.calendar_counts,
            prior=prior_counts,
        )
        typer.echo(f"      pending: {job.pending_digests} digests")


def _render_sp_submit_warning(jobs: tuple[wf_common.JobProgress, ...]) -> None:
    for job in jobs:
        if (
            job.submitted_hashes is not None
            and job.total_documents_found is not None
            and job.total_documents_found < job.submitted_hashes
        ):
            typer.echo(
                f"  Warning: SP recognized {job.total_documents_found} of "
                f"{job.submitted_hashes} submitted hashes"
            )
            return


def render_watch_progress(
    out: Console, kind: str, data: dict, *, next_action: str
) -> None:
    if kind == "already_advanced":
        render_section(out, "Workflow", leading_blank=True)
        typer.echo(f"  Phase: {data['phase']}")
        typer.echo(f"  Next: {next_action}")
        return
    if kind == "block":
        render_section(out, "Block", leading_blank=True)
        typer.echo(f"  Height: {data['height']}")
        typer.echo("  Checking SimpleProof jobs")
        typer.echo(f"  Phase: {data['phase']}")
        _render_jobs(data.get("jobs", ()), prior=data.get("prior_jobs", ()))
        if data.get("reverify"):
            typer.echo("  Rechecking cached proofs after chain movement")
        return
    if kind == "advanced":
        render_section(out, "Workflow", leading_blank=True)
        typer.echo(f"  Phase: {data['previous_phase']} -> {data['phase']}")
        typer.echo(f"  Next: {next_action}")


def render_content_upgrade_result(
    out: Console, object_id: str, result: timestamp.UpgradeResult
) -> None:
    render_section(out, "Content")
    typer.echo(f"  Object: {object_id}")
    typer.echo("  Checked SimpleProof jobs")
    _render_sp_submit_warning(result.jobs)
    _render_jobs(result.jobs)
    if result.unverified_pending_count:
        count = result.unverified_pending_count
        typer.echo(f"  Waiting on {count} pending {pluralize(count, 'digest')}")
    if result.orphan_count:
        count = result.orphan_count
        typer.echo(f"  Held by {count} orphan {pluralize(count, 'digest')}")

    render_section(out, "Workflow", leading_blank=True)
    if result.previous_phase is result.phase:
        typer.echo(f"  Phase: {result.phase.value}")
    else:
        typer.echo(f"  Phase: {result.previous_phase.value} -> {result.phase.value}")
    next_action = (
        "stage timestamp inventory"
        if result.phase is Phase.CONTENT_UPGRADED
        else "stage timestamp content --upgrade --watch"
    )
    typer.echo(f"  Next: {next_action}")


def timestamp_content(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    upgrade: Annotated[
        bool,
        typer.Option(
            "--upgrade",
            help="Poll SP, verify .ots against Bitcoin, ingest companion files.",
        ),
    ] = False,
    watch_flag: Annotated[
        bool,
        typer.Option(
            "--watch",
            help="ZMQ-driven loop; runs an upgrade pass per Bitcoin block.",
        ),
    ] = False,
    retry: Annotated[
        bool,
        typer.Option("--retry", help="Resubmit FAILED submissions only."),
    ] = False,
) -> None:
    """Submit staged content digests to SimpleProof."""
    out = appctx.console(ctx)
    preflight = require_timestamp_preflight(
        ctx,
        object_id,
        upgrade=upgrade,
        watch=watch_flag,
        retry=retry,
    )
    run = _timestamp_run(ctx, preflight, object_id)
    target = TimestampTarget.CONTENT

    if watch_flag:
        _watch(ctx, run, target, next_action="stage timestamp inventory")

    if upgrade:
        result = _halting(lambda: timestamp.upgrade(run, expect=target))
        render_content_upgrade_result(out, object_id, result)
        return

    if retry:
        _retry(run, target)
        return

    result = timestamp.submit(run, expect=target)
    render_section(out, "Content")
    typer.echo(f"  Object: {object_id}")
    if result.submitted_count:
        typer.echo(
            f"  Submitted {result.submitted_count} "
            f"{pluralize(result.submitted_count, 'digest')} to SimpleProof"
        )
    else:
        typer.echo("  Nothing to timestamp")

    render_section(out, "Workflow", leading_blank=True)
    if result.previous_phase is result.phase:
        typer.echo(f"  Phase: {result.phase.value}")
    else:
        typer.echo(f"  Phase: {result.previous_phase.value} -> {result.phase.value}")
    if result.noop_advanced:
        typer.echo("  Next: stage timestamp inventory")
    else:
        typer.echo("  Next: stage timestamp content --upgrade --watch")


def timestamp_inventory(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    upgrade: Annotated[
        bool,
        typer.Option(
            "--upgrade",
            help="Poll SP, verify inventory .ots against Bitcoin, advance phase.",
        ),
    ] = False,
    watch_flag: Annotated[
        bool,
        typer.Option(
            "--watch",
            help="ZMQ-driven loop; runs an inventory upgrade pass per block.",
        ),
    ] = False,
    retry: Annotated[
        bool,
        typer.Option("--retry", help="Re-POST inventory digest at inventory-pending."),
    ] = False,
    allow_missing_metadata: Annotated[
        bool,
        typer.Option(
            "--allow-missing-metadata",
            help="Timestamp inventory even when staged version metadata is incomplete.",
        ),
    ] = False,
) -> None:
    """Build inventory, submit its digest to SimpleProof, serialize plan."""
    preflight = require_timestamp_preflight(
        ctx,
        object_id,
        upgrade=upgrade,
        watch=watch_flag,
        retry=retry,
    )
    validate_inventory_metadata_flag(
        ctx,
        upgrade=upgrade,
        watch=watch_flag,
        retry=retry,
        allow_missing_metadata=allow_missing_metadata,
    )
    run = _timestamp_run(ctx, preflight, object_id)
    target = TimestampTarget.INVENTORY

    if watch_flag:
        _watch(ctx, run, target, next_action="stage commit")

    if upgrade:
        _halting(lambda: timestamp.upgrade(run, expect=target))
        return

    if retry:
        _retry(run, target)
        return

    try:
        timestamp.submit(
            run, expect=target, allow_missing_metadata=allow_missing_metadata
        )
    except PhaseError as e:
        raise ErrorContext(e, suffix="; run 'stage timestamp content' first") from e


def _timestamp_run(
    ctx: typer.Context, preflight: TimestampPreflight, object_id: str
) -> timestamp.TimestampRun:
    sr, _fs = open_root(ctx)
    resolved = preflight.resolved
    return timestamp.TimestampRun(
        cfg=preflight.cfg,
        store=resolved.store,
        sr=sr,
        root_path=resolved.root_path,
        object_id=object_id,
        staging_dir=resolved.staging_dir,
        sp_factory=preflight.sp_factory,
        btc_factory=preflight.btc_factory,
        runtime=appctx.runtime(ctx),
    )


def _halting[T](call: Callable[[], T]) -> T:
    """Run `call`; an attestation mismatch or deep reorg is a halt (exit 5)."""
    try:
        return call()
    except (ReorgDetectedError, AttestationMismatchError) as e:
        raise ErrorContext(e, prefix="halt: ") from e


def _retry(run: timestamp.TimestampRun, target: TimestampTarget) -> None:
    try:
        timestamp.retry(run, expect=target)
    except timestamp.NoFailedSubmissionsError as e:
        typer.echo(str(e))


def _watch(
    ctx: typer.Context,
    run: timestamp.TimestampRun,
    target: TimestampTarget,
    *,
    next_action: str,
) -> NoReturn:
    out = appctx.console(ctx)
    state = wf_staging.ots_state(run.staging_dir)
    render_watch_start(
        out,
        run.object_id,
        wf_common.compute_job_progress(state, runtime=run.runtime),
        waiting=state.phase is not rules_for(target).upgraded_phase,
    )
    exit_code = _halting(
        lambda: watch.watch(
            run,
            expect=target,
            progress=lambda kind, data: render_watch_progress(
                out, kind, data, next_action=next_action
            ),
        )
    )
    raise typer.Exit(exit_code)
