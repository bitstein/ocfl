import os
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, cast

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
    workers = max_workers or min(32, (os.cpu_count() or 1) + 4)
    cap = max_outstanding or 2 * workers

    pool = ThreadPoolExecutor(max_workers=workers)
    pending: list[Future[R]] = []
    it = iter(iterable)

    try:
        for item in it:
            pending.append(pool.submit(fn, item))
            if len(pending) >= cap:
                break

        while pending:
            result = pending.pop(0).result()

            nxt = next(it, _SENTINEL)
            if nxt is not _SENTINEL:
                pending.append(pool.submit(fn, cast("T", nxt)))

            yield result
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
