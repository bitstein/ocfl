"""ZMQ-driven block-watch loops for `stage timestamp content/inventory --watch`.

Per-block invariant: under the staging filelock, decide whether the chain
moved (height jumped or stable-height tip swapped — the reorg/stale-tip
window) and re-verify cached proofs accordingly, then run one upgrade
pass. Returns when the phase advances or the user interrupts.

Receives `sp_factory` / `btc_factory` from the typer callback (sourced
from `ctx.obj`); tests inject fakes by passing the same dict to
`runner.invoke(obj=...)`.
"""

import contextlib
import logging
from typing import TYPE_CHECKING, cast

from ocfl_cli.errors import LockTimeoutError
from ocfl_cli.stage import service
from ocfl_cli.stage.fsm import Phase
from ocfl_cli.stage.state import load_ots, save_ots, with_lock

if TYPE_CHECKING:
    from pathlib import Path

    from ocfl_cli._appctx import BtcRpcFactory, SpClientFactory
    from ocfl_cli.config import Config
    from ocfl_cli.ots.bitcoin import HashBlockStream
    from ocfl_cli.stage.store import StagingStore


logger = logging.getLogger(__name__)


def _get_zmq_subscriber(cfg: Config) -> HashBlockStream:
    """Construct the production ``BitcoinZmqSubscriber`` from `cfg`.

    Tests monkeypatch this seam to inject ``InMemoryHashBlockStream``.
    """
    from ocfl_cli.ots.bitcoin import BitcoinZmqSubscriber

    btc_cfg = cfg.bitcoin
    if btc_cfg is None:
        raise RuntimeError("bitcoin config missing")
    return BitcoinZmqSubscriber(btc_cfg.zmq_endpoint)


def _watch_loop(
    cfg: Config,
    store: StagingStore,
    sr,
    root_path: str,
    object_id: str,
    staging_dir: Path,
    *,
    sp_factory: SpClientFactory,
    btc_factory: BtcRpcFactory,
) -> int:
    """Drive ``timestamp content --upgrade --watch`` end-to-end. Returns exit code."""
    btc_cfg = cfg.bitcoin
    if btc_cfg is None or cfg.simpleproof is None:
        raise RuntimeError("config missing required sections")
    min_confs = btc_cfg.confirmations
    btc = btc_factory(cfg)
    last_height: int | None = None
    last_tip_hash: str | None = None
    stream = _get_zmq_subscriber(cfg)
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
                try:
                    with with_lock(staging_dir, timeout=service._LOCK_TIMEOUT_SECONDS):
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
                            state = service._reverify_existing_proofs(
                                state, btc, sp, staging_dir, min_confs
                            )
                            save_ots(staging_dir, state)
                        last_height = info.height
                        last_tip_hash = info.best_block_hash
                        service._upgrade_pass_locked(
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
                        if state.phase is Phase.CONTENT_UPGRADED:
                            return 0
                except LockTimeoutError as e:
                    logger.warning(
                        "staging lock contended (%s); retrying on next block", e
                    )
                    continue
    except KeyboardInterrupt:
        return 130


def _inventory_watch_loop(
    cfg: Config,
    staging_dir: Path,
    *,
    sp_factory: SpClientFactory,
    btc_factory: BtcRpcFactory,
) -> int:
    """Drive ``timestamp inventory --upgrade --watch``. Returns exit code."""
    btc_cfg = cfg.bitcoin
    if btc_cfg is None or cfg.simpleproof is None:
        raise RuntimeError("config missing required sections")
    min_confs = btc_cfg.confirmations
    btc = btc_factory(cfg)
    last_height: int | None = None
    last_tip_hash: str | None = None
    stream = _get_zmq_subscriber(cfg)
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
                try:
                    with with_lock(staging_dir, timeout=service._LOCK_TIMEOUT_SECONDS):
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
                            state = service._inventory_reverify_existing_proof(
                                state, btc, sp, staging_dir, min_confs
                            )
                            save_ots(staging_dir, state)
                        last_height = info.height
                        last_tip_hash = info.best_block_hash
                        service._inventory_upgrade_pass_locked(
                            cfg,
                            staging_dir,
                            sp_factory=sp_factory,
                            btc_factory=btc_factory,
                        )
                        state = load_ots(staging_dir)
                        if state.phase is Phase.INVENTORY_UPGRADED:
                            return 0
                except LockTimeoutError as e:
                    logger.warning(
                        "staging lock contended (%s); retrying on next block", e
                    )
                    continue
    except KeyboardInterrupt:
        return 130
