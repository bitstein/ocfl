from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ocfl_lib._digest import DigestAlgorithm
    from ocfl_lib.fs._protocols import FS


class ContentSource(Protocol):
    """Resolves content by digest.

    Raises KeyError (e.g. ContentNotFound) when it cannot supply a digest.
    """

    def get_content(self, digest: str) -> tuple[FS, str]: ...


class FixitySource(Protocol):
    """Resolves fixity digests by primary digest."""

    def get_fixity(self, digest: str) -> dict[DigestAlgorithm, str]: ...
