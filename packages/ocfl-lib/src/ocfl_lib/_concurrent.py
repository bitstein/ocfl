"""Bounded concurrent map with fail-fast cancellation."""

from __future__ import annotations

import os
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterable

__all__ = ["bounded_map"]

_SENTINEL = object()


def bounded_map[T, R](
    fn: Callable[[T], R],
    iterable: Iterable[T],
    *,
    max_workers: int | None = None,
    max_outstanding: int | None = None,
) -> Generator[R]:
    """Apply *fn* to each item, yielding results in submission order.

    Limits in-flight futures to *max_outstanding* (default ``2 * workers``)
    so memory stays bounded for large iterables.  On first exception or
    consumer abandon (``GeneratorExit``), pending futures are cancelled
    and the pool is shut down.
    """
    workers = max_workers or min(32, (os.cpu_count() or 1) + 4)
    cap = max_outstanding or 2 * workers

    pool = ThreadPoolExecutor(max_workers=workers)
    pending: list[Future[R]] = []
    it = iter(iterable)

    try:
        # Fill initial window
        for item in it:
            pending.append(pool.submit(fn, item))
            if len(pending) >= cap:
                break

        while pending:
            result = pending.pop(0).result()

            # Refill one slot
            nxt = next(it, _SENTINEL)
            if nxt is not _SENTINEL:
                pending.append(pool.submit(fn, nxt))  # type: ignore[arg-type]

            yield result
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
