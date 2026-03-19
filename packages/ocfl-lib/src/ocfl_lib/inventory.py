"""OCFL inventory model using msgspec structs."""

import json
from dataclasses import dataclass

import msgspec

from ocfl_lib.digest import DigestAlgorithm, bytes_digest


class UserInfo(msgspec.Struct, omit_defaults=True):
    """Version creator information."""

    name: str
    address: str | None = None


class VersionInfo(msgspec.Struct, omit_defaults=True):
    """Single version descriptor within an inventory."""

    created: str
    state: dict[str, list[str]]
    message: str | None = None
    user: UserInfo | None = None


class Inventory(msgspec.Struct, rename="camel", omit_defaults=True):
    """OCFL inventory structure.

    Field names use snake_case in Python but serialize to camelCase JSON
    (e.g. digest_algorithm ↔ digestAlgorithm).
    """

    id: str
    type: str
    digest_algorithm: str
    head: str
    manifest: dict[str, list[str]]
    versions: dict[str, VersionInfo]
    content_directory: str | None = None
    fixity: dict[str, dict[str, list[str]]] | None = None


@dataclass(frozen=True)
class StoredInventory:
    """Inventory paired with its exact serialized bytes and digest.

    Preserves byte-for-byte fidelity for sidecar integrity verification.
    """

    inventory: Inventory
    raw_bytes: bytes
    digest: str
    digest_algorithm: DigestAlgorithm

    @classmethod
    def from_bytes(cls, data: bytes, alg: DigestAlgorithm) -> StoredInventory:
        """Decode inventory from raw JSON bytes, preserving exact bytes."""
        inv = msgspec.json.decode(data, type=Inventory)
        digest = bytes_digest(data, alg)
        return cls(inventory=inv, raw_bytes=data, digest=digest, digest_algorithm=alg)

    @classmethod
    def from_inventory(cls, inv: Inventory, alg: DigestAlgorithm) -> StoredInventory:
        """Encode inventory to compact JSON bytes with sorted keys."""
        obj = msgspec.to_builtins(inv)
        raw = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
        digest = bytes_digest(raw, alg)
        return cls(inventory=inv, raw_bytes=raw, digest=digest, digest_algorithm=alg)

    @property
    def sidecar_filename(self) -> str:
        return f"inventory.json.{self.digest_algorithm.ocfl_name}"

    @property
    def sidecar_content(self) -> str:
        return f"{self.digest} inventory.json\n"
