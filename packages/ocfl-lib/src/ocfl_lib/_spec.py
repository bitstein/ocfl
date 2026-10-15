import enum
import functools
import re
from dataclasses import dataclass
from typing import Literal

from ocfl_lib._errors import InvalidVersionNumber, UnknownInventoryType


class Spec(enum.StrEnum):
    """OCFL specification version."""

    V1_0 = "1.0"
    V1_1 = "1.1"

    @property
    def inventory_type_url(self) -> str:
        return f"https://ocfl.io/{self.value}/spec/#inventory"

    def is_newer_than(self, other: Spec) -> bool:
        """Numeric version comparison (not string order)."""

        def key(s: Spec) -> tuple[int, ...]:
            return tuple(int(p) for p in s.value.split("."))

        return key(self) > key(other)

    @classmethod
    def from_inventory_type(cls, url: str) -> Spec:
        """Parse 'https://ocfl.io/1.1/spec/#inventory' → Spec.V1_1."""
        for member in cls:
            if member.inventory_type_url == url:
                return member
        raise UnknownInventoryType(f"unknown inventory type URL: {url}")


DEFAULT_SPEC: Spec = Spec.V1_1
INVENTORY_FILENAME: str = "inventory.json"
DEFAULT_CONTENT_DIRECTORY: str = "content"
DEFAULT_DIGEST_ALGORITHM: str = "sha512"

VERSION_DIR_RE = re.compile(r"^v(\d+)$")


@functools.total_ordering
@dataclass(frozen=True, order=False)
class VNum:
    """OCFL version number (e.g. v1, v002).

    Per the OCFL spec, a version number is a positive integer (E105).
    Padding is a directory naming convention, not part of the version's
    identity. ``VNum(1, 0)`` and ``VNum(1, 5)`` are the same version with
    different display formatting:

        >>> VNum(1, 0) == VNum(1, 5)
        True
        >>> hash(VNum(1, 0)) == hash(VNum(1, 5))
        True
        >>> str(VNum(1, 0)), str(VNum(1, 5))
        ('v1', 'v00001')
        >>> len({VNum(1, 0), VNum(1, 5)})
        1

    The spec requires that all versions within a single object share the
    same padding (E012/E013); validation enforces this. Padding is
    preserved as a presentation field for round-trip stability (E014):
    parsing ``v001`` and stringifying yields ``v001``. Ordering compares
    only ``number``.
    """

    number: int
    padding: int = 0

    def __post_init__(self):
        if self.number < 1:
            raise InvalidVersionNumber(
                f"version number must be >= 1, got {self.number}"
            )
        if self.padding < 0:
            raise InvalidVersionNumber(f"padding must be >= 0, got {self.padding}")
        if self.padding and len(str(self.number)) >= self.padding:
            raise InvalidVersionNumber(
                f"v{self.number} overflows padding width {self.padding}"
            )

    @classmethod
    def parse(cls, s: str) -> VNum:
        """Parse 'v1' → VNum(1, 0), 'v001' → VNum(1, 3)."""
        m = VERSION_DIR_RE.match(s)
        if not m or not int(m.group(1)):
            raise InvalidVersionNumber(f"invalid version number: {s!r}")
        digits = m.group(1)
        padding = len(digits) if digits[0] == "0" else 0
        return cls(int(digits), padding)

    @classmethod
    def first(cls, padding: int = 0) -> VNum:
        return cls(1, padding)

    def next(self) -> VNum:
        n = self.number + 1
        return VNum(n, self.padding)

    def __str__(self) -> str:
        if self.padding:
            return f"v{self.number:0{self.padding}d}"
        return f"v{self.number}"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, VNum):
            return NotImplemented
        return self.number == other.number

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, VNum):
            return NotImplemented
        return self.number < other.number

    def __hash__(self) -> int:
        return hash(self.number)


_NAMASTE_RE = re.compile(
    rf"^0=(ocfl(?:_object)?)_({'|'.join(re.escape(s) for s in Spec)})$"
)

_NAMASTE_PREFIX = {"root": "ocfl", "object": "ocfl_object"}


@dataclass(frozen=True)
class Namaste:
    """OCFL NAMASTE declaration file."""

    kind: Literal["root", "object"]
    spec: Spec

    @classmethod
    def for_root(cls, spec: Spec) -> Namaste:
        return cls("root", spec)

    @classmethod
    def for_object(cls, spec: Spec) -> Namaste:
        return cls("object", spec)

    @classmethod
    def parse(cls, filename: str) -> Namaste | None:
        """Returns None if not a valid NAMASTE filename."""
        m = _NAMASTE_RE.match(filename)
        if not m:
            return None
        prefix, version = m.group(1), m.group(2)
        kind: Literal["root", "object"] = (
            "object" if prefix == "ocfl_object" else "root"
        )
        return cls(kind, Spec(version))

    @property
    def filename(self) -> str:
        return f"0={_NAMASTE_PREFIX[self.kind]}_{self.spec}"

    @property
    def content(self) -> str:
        return f"{_NAMASTE_PREFIX[self.kind]}_{self.spec}\n"
