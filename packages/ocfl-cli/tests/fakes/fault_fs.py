from collections.abc import Callable
from typing import Any, Protocol

from ocfl_lib.fs import open_fs


class Hook(Protocol):
    def __call__(
        self, name: str, args: tuple[Any, ...], call_real_method: Callable[[], Any]
    ) -> Any: ...


def hooked_fs(location: str, hook: Hook):
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
    fired = False

    def hook(name, args, call_real_method):
        nonlocal fired
        if name == "write" and "/content/" in str(args[0]) and not fired:
            fired = True
            raise exc
        return call_real_method()

    return lambda location: hooked_fs(location, hook)


def before_claim(action: Callable[[], None]) -> Callable[[str], Any]:

    def hook(name, args, call_real_method):
        if name == "rename_noreplace":
            action()
        return call_real_method()

    return lambda location: hooked_fs(location, hook)
