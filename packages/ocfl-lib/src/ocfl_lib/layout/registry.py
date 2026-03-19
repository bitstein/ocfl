"""Layout registry: maps extension names to factory functions."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from ocfl_lib.layout.types import Layout


class LayoutRegistry:
    """Registry mapping extension names to layout factory functions."""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[..., Layout]] = {}

    def register(self, name: str, factory: Callable[..., Layout]) -> None:
        self._factories[name] = factory

    def get_layout(self, name: str, *, config: dict | None = None) -> Layout:
        try:
            factory = self._factories[name]
        except KeyError:
            raise KeyError(f"unknown layout extension: {name!r}") from None
        return factory(config)
