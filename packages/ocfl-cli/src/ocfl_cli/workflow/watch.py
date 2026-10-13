"""The block-watch loop for `stage timestamp|upgrade --watch`.

One upgrade pass runs at the start (proofs already final need not wait for
a block). Per-block invariant: under the staging lock, decide whether the chain
moved (height jumped or stable-height tip swapped — the reorg/stale-tip
window) and re-verify the target's cached proofs accordingly, then run one
upgrade pass. Returns when the target's phase is reached or the user
interrupts. One loop serves both Timestamp Targets: the target comes from
the Phase (or the caller's `expect`).

Blocks come from `runtime.block_streams` (ZMQ `hashblock` in production)
and the wait for SimpleProof after each block is `runtime.sleeper`; the
clients come from `sp_factory` / `btc_factory`. All are ports from `AppCtx`,
so tests inject fakes through `runner.invoke(obj=...)`.
"""

import contextlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from ocfl_cli.errors import LockTimeoutError
from ocfl_cli.model.phase import TimestampTarget  # noqa: TC001 - signature() reads it.
from ocfl_cli.store.ots_state import apply_category_id, load_ots, save_ots, with_lock
from ocfl_cli.workflow import timestamp

if TYPE_CHECKING:
    from ocfl_cli.ports import BlockStream


logger = logging.getLogger(__name__)

type WatchProgress = Callable[[str, dict[str, Any]], None]

BLOCK_PROCESSING_DELAY_SECONDS = 60.0


def watch(
    run: timestamp.TimestampRun,
    *,
    expect: TimestampTarget | None = None,
    progress: WatchProgress | None = None,
) -> int:
    """Upgrade on each block until the target's proofs are final; the exit code."""
    runtime = run.runtime
    btc = run.btc_factory(run.cfg)
    min_confs = run.min_confirmations
    state = load_ots(run.staging_dir)
    rules = timestamp.resolve(state, expect)
    if state.phase is rules.upgraded_phase:
        if progress is not None:
            progress(
                "already_advanced",
                {
                    "phase": state.phase.value,
                    "jobs": timestamp.target_jobs(state, rules, runtime=runtime),
                },
            )
        return 0
    last_height: int | None = None
    last_tip_hash: str | None = None
    prior_jobs: tuple[timestamp.JobProgress, ...] = ()
    stream = runtime.block_streams(run.cfg)
    # Real BitcoinZmqSubscriber is a context manager (closes the socket on
    # exit); test fakes implement only `wait_for_block`. nullcontext bridges
    # both with one `with` site.
    stream_cm = cast(
        "contextlib.AbstractContextManager[BlockStream]",
        stream if hasattr(stream, "__enter__") else contextlib.nullcontext(stream),
    )
    try:
        with stream_cm as blocks:
            # One pass before the first block: proofs that are already final
            # need not wait for the next one. Subscribed first, so no block
            # is missed meanwhile.
            try:
                with with_lock(run.staging_dir, runtime):
                    before = load_ots(run.staging_dir)
                    timestamp.upgrade_locked(run, expect=rules.target)
                    state = load_ots(run.staging_dir)
            except LockTimeoutError as e:
                logger.warning("staging lock contended (%s); waiting for a block", e)
            else:
                prior_jobs = timestamp.target_jobs(state, rules, runtime=runtime)
                if state.phase is rules.upgraded_phase:
                    if progress is not None:
                        progress(
                            "advanced",
                            {
                                "previous_phase": before.phase.value,
                                "phase": state.phase.value,
                            },
                        )
                    return 0
            while True:
                if blocks.wait_for_block(timeout_ms=5000) is None:
                    continue
                runtime.sleeper.sleep(BLOCK_PROCESSING_DELAY_SECONDS)
                try:
                    with with_lock(run.staging_dir, runtime):
                        info = btc.getblockchaininfo()
                        moved = last_height is not None and (
                            info.height - last_height > 1
                            or (
                                info.height == last_height
                                and last_tip_hash is not None
                                and last_tip_hash != info.best_block_hash
                            )
                        )
                        if moved:
                            state = load_ots(run.staging_dir)
                            state = timestamp.reverify(
                                state,
                                rules.reverify_digests(state),
                                btc=btc,
                                sp=run.sp_factory(run.cfg),
                                staging_dir=run.staging_dir,
                                min_confirmations=min_confs,
                                category_id=apply_category_id(
                                    state, run.category_config
                                ),
                            )
                            save_ots(run.staging_dir, state)
                        before = load_ots(run.staging_dir)
                        last_height = info.height
                        last_tip_hash = info.best_block_hash
                        timestamp.upgrade_locked(run, expect=rules.target)
                        state = load_ots(run.staging_dir)
                        jobs = timestamp.target_jobs(state, rules, runtime=runtime)
                        if progress is not None:
                            progress(
                                "block",
                                {
                                    "height": info.height,
                                    "phase": before.phase.value,
                                    "reverify": moved,
                                    "jobs": jobs,
                                    "prior_jobs": prior_jobs,
                                },
                            )
                        prior_jobs = jobs
                        if state.phase is rules.upgraded_phase:
                            if progress is not None:
                                progress(
                                    "advanced",
                                    {
                                        "previous_phase": before.phase.value,
                                        "phase": state.phase.value,
                                    },
                                )
                            return 0
                except LockTimeoutError as e:
                    logger.warning(
                        "staging lock contended (%s); retrying on next block", e
                    )
                    continue
    except KeyboardInterrupt:
        return 130
