from collections.abc import Iterator

import httpx2 as httpx


def exc_chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__ or cur.__context__


def exc_chain_text(exc: BaseException) -> str:
    return "\n".join(f"{e!r}\n{e}" for e in exc_chain(exc))


def exc_chain_request_headers(exc: BaseException) -> list[httpx.Headers]:
    requests = (getattr(e, "request", None) for e in exc_chain(exc))
    return [req.headers for req in requests if req is not None]
