"""ZMQ-driven block-watch loops for `stage timestamp content/inventory --watch`.

Per-block invariant: under the staging filelock, decide whether the chain
moved (height jumped or stable-height tip swapped — the reorg/stale-tip
window) and re-verify cached proofs accordingly, then run one upgrade
pass. Returns when the phase advances or the user interrupts.

Receives `sp_factory` / `btc_factory` from the command (sourced from
`AppCtx`); tests inject fakes by passing them in `runner.invoke(obj=...)`.
"""

import contextlib
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from ocfl_cli.errors import LockTimeoutError
from ocfl_cli.model.phase import Phase
from ocfl_cli.store import ots_state as ots_state
from ocfl_cli.store.ots_state import load_ots, save_ots, with_lock
from ocfl_cli.workflow import common as wf_common
from ocfl_cli.workflow import content as wf_content
from ocfl_cli.workflow import inventory as wf_inventory

if TYPE_CHECKING:
    from pathlib import Path

    from ocfl_cli.config import Config
    from ocfl_cli.ports import BtcRpcFactory, HashBlockStream, SpClientFactory
    from ocfl_cli.store.staging import StagingStore


logger = logging.getLogger(__name__)

type WatchProgress = Callable[[str, dict[str, Any]], None]

_BLOCK_PROCESSING_DELAY_SECONDS = 60.0


def block_stream_for(cfg: Config) -> HashBlockStream:
    """Construct the production ``BitcoinZmqSubscriber`` from `cfg`.

    Tests monkeypatch this seam to inject ``InMemoryHashBlockStream``.
    """
    from ocfl_cli.ots.bitcoin import BitcoinZmqSubscriber

    btc_cfg = cfg.bitcoin
    if btc_cfg is None:
        raise RuntimeError("bitcoin config missing")
    return BitcoinZmqSubscriber(btc_cfg.zmq_endpoint)


def sleep_after_block() -> None:
    """Give SimpleProof time to process newly mined blocks before polling."""
    time.sleep(_BLOCK_PROCESSING_DELAY_SECONDS)


def watch_content(
    cfg: Config,
    store: StagingStore,
    sr,
    root_path: str,
    object_id: str,
    staging_dir: Path,
    *,
    sp_factory: SpClientFactory,
    btc_factory: BtcRpcFactory,
    progress: WatchProgress | None = None,
) -> int:
    """Drive ``timestamp content --upgrade --watch`` end-to-end. Returns exit code."""
    btc_cfg = cfg.bitcoin
    if btc_cfg is None or cfg.simpleproof is None:
        raise RuntimeError("config missing required sections")
    min_confs = btc_cfg.confirmations
    btc = btc_factory(cfg)
    last_height: int | None = None
    last_tip_hash: str | None = None
    prior_jobs: tuple[wf_common.JobProgress, ...] = ()
    state = load_ots(staging_dir)
    if state.phase is Phase.CONTENT_UPGRADED:
        if progress is not None:
            progress(
                "already_advanced",
                {
                    "phase": state.phase.value,
                    "jobs": wf_common.compute_job_progress(state),
                },
            )
        return 0
    stream = block_stream_for(cfg)
    # Real BitcoinZmqSubscriber is a context manager (closes the socket on
    # exit); test fakes implement only `wait_for_block`. nullcontext bridges
    # both with one `with` site.
    stream_cm = cast(
        "contextlib.AbstractContextManager[HashBlockStream]",
        stream if hasattr(stream, "__enter__") else contextlib.nullcontext(stream),
    )
    try:
        with stream_cm as zmq_sub:
            while True:
                block = zmq_sub.wait_for_block(timeout_ms=5000)
                if block is None:
                    continue
                sleep_after_block()
                try:
                    with with_lock(staging_dir, timeout=wf_common.LOCK_TIMEOUT_SECONDS):
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
                        if height_jumped or tip_changed_at_stable:
                            sp = sp_factory(cfg)
                            state = load_ots(staging_dir)
                            category_id = ots_state.apply_category_id(
                                state, cfg.simpleproof.proof_category_id
                            )
                            state = wf_content.reverify_content_proofs(
                                state, btc, sp, staging_dir, min_confs, category_id
                            )
                            save_ots(staging_dir, state)
                        before = load_ots(staging_dir)
                        last_height = info.height
                        last_tip_hash = info.best_block_hash
                        wf_content.upgrade_content_locked(
                            cfg,
                            store,
                            sr,
                            root_path,
                            object_id,
                            staging_dir,
                            sp_factory=sp_factory,
                            btc_factory=btc_factory,
                        )
                        state = load_ots(staging_dir)
                        jobs = wf_common.compute_job_progress(state)
                        if progress is not None:
                            progress(
                                "block",
                                {
                                    "height": info.height,
                                    "phase": before.phase.value,
                                    "reverify": height_jumped or tip_changed_at_stable,
                                    "jobs": jobs,
                                    "prior_jobs": prior_jobs,
                                },
                            )
                        prior_jobs = jobs
                        if state.phase is Phase.CONTENT_UPGRADED:
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


def watch_inventory(
    cfg: Config,
    staging_dir: Path,
    *,
    sp_factory: SpClientFactory,
    btc_factory: BtcRpcFactory,
    progress: WatchProgress | None = None,
) -> int:
    """Drive ``timestamp inventory --upgrade --watch``. Returns exit code."""
    btc_cfg = cfg.bitcoin
    if btc_cfg is None or cfg.simpleproof is None:
        raise RuntimeError("config missing required sections")
    min_confs = btc_cfg.confirmations
    btc = btc_factory(cfg)
    last_height: int | None = None
    last_tip_hash: str | None = None
    prior_jobs: tuple[wf_common.JobProgress, ...] = ()
    state = load_ots(staging_dir)
    if state.phase is Phase.INVENTORY_UPGRADED:
        if progress is not None:
            progress(
                "already_advanced",
                {
                    "phase": state.phase.value,
                    "jobs": wf_common.compute_job_progress(state),
                },
            )
        return 0
    stream = block_stream_for(cfg)
    stream_cm = cast(
        "contextlib.AbstractContextManager[HashBlockStream]",
        stream if hasattr(stream, "__enter__") else contextlib.nullcontext(stream),
    )
    try:
        with stream_cm as zmq_sub:
            while True:
                block = zmq_sub.wait_for_block(timeout_ms=5000)
                if block is None:
                    continue
                sleep_after_block()
                try:
                    with with_lock(staging_dir, timeout=wf_common.LOCK_TIMEOUT_SECONDS):
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
                        if height_jumped or tip_changed_at_stable:
                            sp = sp_factory(cfg)
                            state = load_ots(staging_dir)
                            category_id = ots_state.apply_category_id(
                                state, cfg.simpleproof.proof_category_id
                            )
                            state = wf_inventory.reverify_inventory_proof(
                                state, btc, sp, staging_dir, min_confs, category_id
                            )
                            save_ots(staging_dir, state)
                        before = load_ots(staging_dir)
                        last_height = info.height
                        last_tip_hash = info.best_block_hash
                        wf_inventory.upgrade_inventory_locked(
                            cfg,
                            staging_dir,
                            sp_factory=sp_factory,
                            btc_factory=btc_factory,
                        )
                        state = load_ots(staging_dir)
                        jobs = wf_common.compute_job_progress(state)
                        if progress is not None:
                            progress(
                                "block",
                                {
                                    "height": info.height,
                                    "phase": before.phase.value,
                                    "reverify": height_jumped or tip_changed_at_stable,
                                    "jobs": jobs,
                                    "prior_jobs": prior_jobs,
                                },
                            )
                        prior_jobs = jobs
                        if state.phase is Phase.INVENTORY_UPGRADED:
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
