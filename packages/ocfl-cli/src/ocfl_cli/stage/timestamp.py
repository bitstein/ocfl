"""stage timestamp, upgrade and retry: one verb each, the target from the Phase.

Each verb works on the Timestamp Target the Phase is at; `--expect` asserts
which one (a mismatch is exit 3). A verb with nothing to do says what to run
next and exits 0. Under `--json` a one-shot verb prints one document and
`--watch` prints NDJSON events ending in a `summary` event.
"""

import json
from typing import TYPE_CHECKING, Annotated, Any

import typer

from ocfl_cli import _appctx as appctx
from ocfl_cli._common import open_root, output_json
from ocfl_cli.errors import (
    AttestationMismatchError,
    ErrorContext,
    PhaseError,
    ReorgDetectedError,
)
from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.model.phase import TimestampTarget
from ocfl_cli.stage._app import TimestampPreflight, require_timestamp_preflight
from ocfl_cli.stage.render import pluralize, render_section, short_job_id
from ocfl_cli.workflow import common as wf_common
from ocfl_cli.workflow import staging as wf_staging
from ocfl_cli.workflow import status as wf_status
from ocfl_cli.workflow import timestamp, watch
from ocfl_cli.workflow.targets import rules_for

if TYPE_CHECKING:
    from collections.abc import Callable

    from rich.console import Console

    from ocfl_cli.model.phase import Phase

type Outcome = (
    timestamp.SubmitResult
    | timestamp.UpgradeResult
    | timestamp.RetryResult
    | timestamp.Idle
)

ObjectArg = Annotated[str, typer.Argument(help="Object ID.")]
ExpectOption = Annotated[
    TimestampTarget | None,
    typer.Option(
        "--expect",
        help="Fail (exit 3) unless the Phase is at this Timestamp Target.",
    ),
]
WatchOption = Annotated[
    bool,
    typer.Option(
        "--watch",
        help="Then upgrade on each Bitcoin block until the proofs are final.",
    ),
]


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
    out: Console, kind: str, data: dict, *, next_action: Callable[[], str]
) -> None:
    if kind == "already_advanced":
        render_section(out, "Workflow", leading_blank=True)
        typer.echo(f"  Phase: {data['phase']}")
        typer.echo(f"  Next: {next_action()}")
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
        typer.echo(f"  Next: {next_action()}")


def render_upgrade_result(
    out: Console, object_id: str, result: timestamp.UpgradeResult
) -> None:
    render_section(out, result.target.value.title())
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


def _render_workflow(before: Phase, after: Phase, next_action: str) -> None:
    if before is after:
        typer.echo(f"  Phase: {after.value}")
    else:
        typer.echo(f"  Phase: {before.value} -> {after.value}")
    typer.echo(f"  Next: {next_action}")


def _document(
    run: timestamp.TimestampRun,
    verb: str,
    outcome: Outcome,
    *,
    before: Phase,
    next_action: str,
) -> dict[str, Any]:
    after = outcome.phase
    doc: dict[str, Any] = {
        "object_id": run.object_id,
        "verb": verb,
        "target": outcome.target.value,
        "phase_before": before.value,
        "phase_after": after.value,
        "done": outcome.done,
        "idle": isinstance(outcome, timestamp.Idle),
        "next": next_action,
    }
    match outcome:
        case timestamp.SubmitResult():
            doc["submitted"] = outcome.submitted_count
        case timestamp.UpgradeResult():
            doc["pending"] = outcome.unverified_pending_count
            doc["unrequested"] = outcome.orphan_count
        case timestamp.RetryResult():
            doc["retried"] = outcome.retried
        case timestamp.Idle():
            pass
    return doc


def _render(
    ctx: typer.Context,
    run: timestamp.TimestampRun,
    verb: str,
    outcome: Outcome,
    *,
    before: Phase,
) -> None:
    next_action = timestamp.next_step(run)
    if appctx.json_mode(ctx):
        output_json(
            _document(run, verb, outcome, before=before, next_action=next_action)
        )
        return
    out = appctx.console(ctx)
    after = before
    match outcome:
        case timestamp.Idle():
            render_section(out, outcome.target.value.title())
            typer.echo(f"  Object: {run.object_id}")
            typer.echo(f"  Nothing to {verb} in phase {outcome.phase.value}")
        case timestamp.SubmitResult():
            after = outcome.phase
            render_section(out, outcome.target.value.title())
            typer.echo(f"  Object: {run.object_id}")
            count = outcome.submitted_count
            if count:
                typer.echo(
                    f"  Submitted {count} {pluralize(count, 'digest')} to SimpleProof"
                )
            else:
                typer.echo("  Nothing to timestamp")
        case timestamp.UpgradeResult():
            after = outcome.phase
            render_upgrade_result(out, run.object_id, outcome)
        case timestamp.RetryResult():
            after = outcome.phase
            render_section(out, outcome.target.value.title())
            typer.echo(f"  Object: {run.object_id}")
            if outcome.retried:
                typer.echo(
                    f"  Resubmitted {outcome.retried} failed "
                    f"{pluralize(outcome.retried, 'submission')}"
                )
            else:
                typer.echo("  no failed submissions to retry")
    render_section(out, "Workflow", leading_blank=True)
    _render_workflow(before, after, next_action)


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


def _run_for(
    ctx: typer.Context, object_id: str, *, bitcoin: bool
) -> timestamp.TimestampRun:
    return _timestamp_run(
        ctx,
        require_timestamp_preflight(ctx, object_id, bitcoin=bitcoin),
        object_id,
    )


def _phase(run: timestamp.TimestampRun) -> Phase:
    return wf_staging.ots_state(run.staging_dir).phase


def _verb[T](run: timestamp.TimestampRun, call: Callable[[], T]) -> T:
    """Run a verb: a halt is exit 5; a phase error names the next command."""
    try:
        return call()
    except (ReorgDetectedError, AttestationMismatchError) as e:
        raise ErrorContext(e, prefix="halt: ") from e
    except PhaseError as e:
        raise ErrorContext(e, suffix=f"; next: {timestamp.next_step(run)}") from e


def _watch(
    ctx: typer.Context,
    run: timestamp.TimestampRun,
    verb: str,
    target: TimestampTarget,
    *,
    before: Phase,
) -> None:
    """Upgrade `target` on each block until its proofs are final."""
    json_mode = appctx.json_mode(ctx)
    out = appctx.console(ctx)
    state = wf_staging.ots_state(run.staging_dir)

    def progress(kind: str, data: dict[str, Any]) -> None:
        if json_mode:
            event = {k: v for k, v in data.items() if k not in {"jobs", "prior_jobs"}}
            typer.echo(
                json.dumps(
                    {"event": kind, "object_id": run.object_id, **event},
                    separators=(",", ":"),
                )
            )
        else:
            render_watch_progress(
                out, kind, data, next_action=lambda: timestamp.next_step(run)
            )

    if not json_mode:
        render_watch_start(
            out,
            run.object_id,
            wf_common.compute_job_progress(state, runtime=run.runtime),
            waiting=state.phase is not rules_for(target).upgraded_phase,
        )
    code = _verb(run, lambda: watch.watch(run, expect=target, progress=progress))
    if json_mode:
        after = _phase(run)
        summary = {
            "event": "summary",
            "object_id": run.object_id,
            "verb": verb,
            "target": target.value,
            "phase_before": before.value,
            "phase_after": after.value,
            "done": rules_for(target).upgraded_phase is after,
            "next": timestamp.next_step(run),
        }
        typer.echo(json.dumps(summary, separators=(",", ":")))
    if code:
        raise typer.Exit(code)


def timestamp_cmd(
    ctx: typer.Context,
    object_id: ObjectArg,
    expect: ExpectOption = None,
    watch_flag: WatchOption = False,
    allow_missing_metadata: Annotated[
        bool,
        typer.Option(
            "--allow-missing-metadata",
            help="Timestamp the inventory even when staged metadata is incomplete.",
        ),
    ] = False,
) -> None:
    """Submit the digests the Phase needs proofs for to SimpleProof.

    Drafting: the content files. Content-upgraded: the inventory.
    """
    run = _run_for(ctx, object_id, bitcoin=watch_flag)
    before = _phase(run)
    result = _verb(
        run,
        lambda: timestamp.submit(
            run, expect=expect, allow_missing_metadata=allow_missing_metadata
        ),
    )
    if watch_flag and not result.done:
        _watch(ctx, run, "timestamp", result.target, before=before)
        return
    _render(ctx, run, "timestamp", result, before=before)


def upgrade_cmd(
    ctx: typer.Context,
    object_id: ObjectArg,
    expect: ExpectOption = None,
    watch_flag: WatchOption = False,
) -> None:
    """Fetch the proofs SimpleProof has made and verify them against Bitcoin."""
    run = _run_for(ctx, object_id, bitcoin=True)
    before = _phase(run)
    idle = _verb(run, lambda: timestamp.idle(run, "upgrade", expect=expect))
    if idle is not None:
        _render(ctx, run, "upgrade", idle, before=before)
        return
    if watch_flag:
        target = _verb(run, lambda: timestamp.check(run, "upgrade", expect=expect))
        _watch(ctx, run, "upgrade", target, before=before)
        return
    result = _verb(run, lambda: timestamp.upgrade(run, expect=expect))
    _render(ctx, run, "upgrade", result, before=before)
    if not result.done:
        raise typer.Exit(ExitCode.PENDING)  # run again later (ADR-0006)


def retry_cmd(
    ctx: typer.Context,
    object_id: ObjectArg,
    expect: ExpectOption = None,
) -> None:
    """Resubmit the failed SimpleProof submissions of the Phase's target."""
    run = _run_for(ctx, object_id, bitcoin=False)
    before = _phase(run)
    idle = _verb(run, lambda: timestamp.idle(run, "retry", expect=expect))
    if idle is not None:
        _render(ctx, run, "retry", idle, before=before)
        return
    result = _verb(run, lambda: timestamp.retry(run, expect=expect))
    _render(ctx, run, "retry", result, before=before)
