from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Self, cast

READS = frozenset({"open_file", "read_dir", "exists", "walk_files", "read_tagged"})


class Crash(BaseException): ...


@dataclass
class Counter:
    writes: int = 0
    crashed: bool = False


def fail_at[F](fs: F, n: int | None) -> tuple[F, Counter]:
    """Wrap `fs` so its `n`-th write raises Crash, and every write after it.

    A write is any public method call that is not in READS (so a lock or an
    rmdir counts). The wrapper is an instance of a subclass of `fs`'s class,
    so backend checks (ClaimFS, ConditionalFS) see the real backend. With
    `n=None` it only counts.
    """
    counter = Counter()

    def __getattribute__(self, name):
        attr = object.__getattribute__(self, name)
        if name.startswith("_") or name in READS or not callable(attr):
            return attr

        def write(*args, **kwargs):
            counter.writes += 1
            if counter.crashed or (n is not None and counter.writes >= n):
                counter.crashed = True
                raise Crash(f"write {counter.writes}: {name}{args[:1]}")
            return attr(*args, **kwargs)

        return write

    cls = type(fs)
    faulty = type(
        f"FailAt{cls.__name__}", (cls,), {"__getattribute__": __getattribute__}
    )
    wrapped = cast("F", object.__new__(faulty))
    wrapped.__dict__.update(fs.__dict__)
    return wrapped, counter


_ABSENT = object()


@dataclass
class CrashBoundary:
    owner: object
    name: str
    when: Callable[..., bool] = lambda *args, **kwargs: True
    crashed: bool = False
    _saved: object = field(default=_ABSENT, init=False, repr=False)

    def __enter__(self) -> Self:
        real = getattr(self.owner, self.name)
        self._saved = vars(self.owner).get(self.name, _ABSENT)

        def call(*args: Any, **kwargs: Any) -> Any:
            if self.when(*args, **kwargs):
                self.crashed = True
                raise Crash(f"{self.name}{args[:1]}")
            return real(*args, **kwargs)

        setattr(self.owner, self.name, call)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._saved is _ABSENT:
            delattr(self.owner, self.name)
        else:
            setattr(self.owner, self.name, self._saved)
