"""A storage filesystem fake for the `fs_factory` port: hooks on chosen calls.

`tests/helpers/faults.py` simulates a dead process (`Crash`, no undo). These
CLI tests need the commit's own failure paths instead: an exception the
commit reverts from, or another writer acting just before a step.
"""

from collections.abc import Callable
from typing import Any

from ocfl_lib.fs import open_fs

# hook(name, args, call) -> result; `call()` runs the real method.
type Hook = Callable[[str, tuple[Any, ...], Callable[[], Any]], Any]


def hooked_fs(location: str, hook: Hook):
    """The real backend at `location`, with every public call going through `hook`.

    The wrapper subclasses the backend, so backend checks (ClaimFS,
    ConditionalFS) still see the real one.
    """
    fs = open_fs(location)

    def __getattribute__(self, name):
        attr = object.__getattribute__(self, name)
        if name.startswith("_") or not callable(attr):
            return attr

        def call(*args, **kwargs):
            return hook(name, args, lambda: attr(*args, **kwargs))

        return call

    cls = type(fs)
    hooked = type(
        f"Hooked{cls.__name__}", (cls,), {"__getattribute__": __getattribute__}
    )
    wrapped = object.__new__(hooked)
    wrapped.__dict__.update(fs.__dict__)
    return wrapped


def raise_on_content_write(exc: BaseException) -> Callable[[str], Any]:
    """An `fs_factory` whose first write of version content raises `exc`.

    The commit is building `vN/content/` in the work dir: nothing is claimed
    yet, so the commit reverts and the object is untouched.
    """
    fired = False

    def hook(name, args, call):
        nonlocal fired
        if name == "write" and "/content/" in str(args[0]) and not fired:
            fired = True
            raise exc
        return call()

    return lambda location: hooked_fs(location, hook)


def before_claim(action: Callable[[], None]) -> Callable[[str], Any]:
    """An `fs_factory` that runs `action` just before the commit's claim."""

    def hook(name, args, call):
        if name == "rename_noreplace":
            action()
        return call()

    return lambda location: hooked_fs(location, hook)
