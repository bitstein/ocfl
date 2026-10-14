from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from ocfl_lib._errors import UnknownLayout

if TYPE_CHECKING:
    from ocfl_lib._layout.types import Layout

type LayoutFactory = Callable[[dict[str, Any] | None], Layout]


@dataclass(frozen=True, slots=True, eq=False)
class LayoutRegistry:
    """Immutable mapping of extension names to layout factories.

    Extend with `with_layout`, which returns a new registry.
    """

    _factories: Mapping[str, LayoutFactory] = field(default_factory=dict)

    def __post_init__(self) -> None:
        proxy = MappingProxyType(dict(self._factories))
        object.__setattr__(self, "_factories", proxy)

    def __contains__(self, name: object) -> bool:
        return name in self._factories

    def __iter__(self) -> Iterator[str]:
        return iter(self._factories)

    def with_layout(self, name: str, factory: LayoutFactory) -> LayoutRegistry:
        """Return a new registry that also maps `name` to `factory`."""
        return LayoutRegistry({**self._factories, name: factory})

    def get_layout(self, name: str, *, config: dict[str, Any] | None = None) -> Layout:
        try:
            factory = self._factories[name]
        except KeyError:
            raise UnknownLayout(name) from None
        return factory(config)
