"""Minimal retry helper keyed on the `Transient` marker mixin.

Non-`Transient` exceptions propagate on first raise (fail fast). For
`Transient` exceptions, the helper sleeps between attempts using
`min(60, 2 * 2**i)`; a per-instance `retry_after` overrides the sleep
with that exact value.
"""

import logging
import time
from typing import TYPE_CHECKING

from ocfl_cli.errors import Transient

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


def retry[T](
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    backoff: tuple[float, ...] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if not isinstance(exc, Transient) or i == attempts - 1:
                raise
            retry_after = exc.retry_after
            if retry_after is not None:
                delay = retry_after
            else:
                base = backoff[i] if backoff is not None else min(60.0, 2.0 * 2**i)
                delay = base
            logger.warning(
                "retrying after transient error: %s (sleep=%.3fs, attempt=%d/%d)",
                exc,
                delay,
                i + 1,
                attempts,
            )
            sleep(delay)
    raise RuntimeError("unreachable")
