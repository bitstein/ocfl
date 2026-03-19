"""Core OCFL types and constants."""

import enum
import re
from dataclasses import dataclass


class Spec(enum.StrEnum):
    """OCFL specification version."""

    V1_0 = "1.0"
    V1_1 = "1.1"

    @property
    def inventory_type_url(self) -> str:
        return f"https://ocfl.io/{self.value}/spec/#inventory"

    @classmethod
    def from_inventory_type(cls, url: str) -> Spec:
        """Parse 'https://ocfl.io/1.1/spec/#inventory' → Spec.V1_1."""
        for member in cls:
            if member.inventory_type_url == url:
                return member
        raise ValueError(f"unknown inventory type URL: {url}")


DEFAULT_SPEC: Spec = Spec.V1_1
INVENTORY_FILENAME: str = "inventory.json"
DEFAULT_CONTENT_DIRECTORY: str = "content"
DEFAULT_DIGEST_ALGORITHM: str = "sha512"

_VNUM_RE = re.compile(r"^v(0*[1-9]\d*)$")


@dataclass(frozen=True, order=False)
class VNum:
    """OCFL version number (e.g. v1, v002)."""

    number: int
    padding: int = 0

    def __post_init__(self):
        if self.number < 1:
            raise ValueError(f"version number must be >= 1, got {self.number}")
        if self.padding < 0:
            raise ValueError(f"padding must be >= 0, got {self.padding}")

    @classmethod
    def parse(cls, s: str) -> VNum:
        """Parse 'v1' → VNum(1, 0), 'v001' → VNum(1, 3)."""
        m = _VNUM_RE.match(s)
        if not m:
            raise ValueError(f"invalid version number: {s!r}")
        digits = m.group(1)
        padding = len(digits) if digits[0] == "0" else 0
        return cls(int(digits), padding)

    @classmethod
    def first(cls, padding: int = 0) -> VNum:
        return cls(1, padding)

    def next(self) -> VNum:
        n = self.number + 1
        if self.padding and len(str(n)) > self.padding:
            raise ValueError(f"v{n} overflows padding width {self.padding}")
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
