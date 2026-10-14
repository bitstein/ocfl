"""Introspection for the docs-guard tests: command help and public docstrings."""

import inspect
from collections.abc import Iterator
from types import ModuleType
from typing import Any


def iter_commands(cmd: Any, path: str) -> Iterator[tuple[str, Any]]:
    """`(path, command)` for `cmd` and every subcommand below it."""
    yield path, cmd
    for name, sub in getattr(cmd, "commands", {}).items():
        yield from iter_commands(sub, f"{path} {name}")


def has_own_docstring(obj: Any) -> bool:
    """False for no docstring and for the one `@dataclass` generates."""
    doc = obj.__doc__
    return bool(doc and doc.strip()) and not doc.startswith(f"{obj.__name__}(")


def undocumented_exports(module: ModuleType) -> list[str]:
    return [
        name
        for name in module.__all__
        if (inspect.isclass(obj := getattr(module, name)) or inspect.isfunction(obj))
        and not has_own_docstring(obj)
    ]


def documented_members(module: ModuleType) -> set[str]:
    """`Class.member` names of the documented public methods and properties."""
    found = set()
    for name in module.__all__:
        cls = getattr(module, name)
        if not inspect.isclass(cls):
            continue
        for attr, value in vars(cls).items():
            if attr.startswith("_"):
                continue
            func = value.fget if isinstance(value, property) else value
            func = getattr(func, "__func__", func)
            if inspect.isfunction(func) and func.__doc__ and func.__doc__.strip():
                found.add(f"{name}.{attr}")
    return found
