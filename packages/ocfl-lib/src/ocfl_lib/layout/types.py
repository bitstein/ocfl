"""Layout protocol definition."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class Layout(Protocol):
    """Protocol for OCFL storage layout extensions."""

    def resolve(self, object_id: str) -> str:
        """Map an object ID to its storage path relative to the root."""
        ...

    @property
    def name(self) -> str:
        """The registered extension name."""
        ...

    @property
    def config(self) -> dict | None:
        """Layout configuration as a dict, or None if no config needed."""
        ...
