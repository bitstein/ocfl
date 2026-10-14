import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ocfl_cli.errors import LockTimeoutError
from ocfl_cli.store.ots_state import (
    load_ots,
    persisted_category_id,
    save_ots,
    with_lock,
)
from ocfl_cli.workflow import timestamp

if TYPE_CHECKING:
    from ocfl_cli.model.phase import TimestampTarget


logger = logging.getLogger(__name__)

type WatchProgress = Callable[[str, dict[str, Any]], None]

BLOCK_PROCESSING_DELAY_SECONDS = 60.0


def watch(
    run: timestamp.TimestampRun,
    *,
    expect: TimestampTarget | None = None,
    progress: WatchProgress | None = None,
) -> int:
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
    try:
        with runtime.block_streams(run.cfg) as blocks:
            try:
                with with_lock(run.staging_dir, runtime) as staging:
                    before = load_ots(staging)
                    timestamp.upgrade_locked(run, staging, expect=rules.target)
                    state = load_ots(staging)
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
                    with with_lock(run.staging_dir, runtime) as staging:
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
                            state = load_ots(staging)
                            state = timestamp.reverify(
                                state,
                                rules.reverify_digests(state),
                                btc=btc,
                                sp=run.sp_factory(run.cfg),
                                staging_dir=staging,
                                min_confirmations=min_confs,
                                category_id=persisted_category_id(
                                    state, run.category_config
                                ),
                            )
                            save_ots(staging, state)
                        before = load_ots(staging)
                        last_height = info.height
                        last_tip_hash = info.best_block_hash
                        timestamp.upgrade_locked(run, staging, expect=rules.target)
                        state = load_ots(staging)
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
