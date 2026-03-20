"""OCFL staging for version assembly."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ocfl_lib.digest import DigestAlgorithm
    from ocfl_lib.digest_map import DigestMap
    from ocfl_lib.update_plan import ContentSource


@runtime_checkable
class FixitySource(Protocol):
    """Protocol for resolving fixity digests by primary digest."""

    def get_fixity(self, digest: str) -> dict[DigestAlgorithm, str]: ...


@dataclass
class Stage:
    """Staged content for creating or updating an OCFL object version."""

    state: DigestMap
    digest_algorithm: DigestAlgorithm
    content_source: ContentSource
    fixity_source: FixitySource | None = None
