"""Core OCFL types and constants."""

import enum


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
