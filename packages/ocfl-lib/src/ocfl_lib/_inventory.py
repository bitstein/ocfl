"""OCFL inventory model using msgspec structs."""

import json
from dataclasses import dataclass

import msgspec

from ocfl_lib._digest import DigestAlgorithm, bytes_digest


class UserInfo(msgspec.Struct, frozen=True, omit_defaults=True):
    """Version creator information."""

    name: str
    address: str | None = None


class VersionInfo(msgspec.Struct, frozen=True, omit_defaults=True):
    """Single version descriptor within an inventory."""

    created: str
    state: dict[str, list[str]]
    message: str | None = None
    user: UserInfo | None = None


class Inventory(msgspec.Struct, frozen=True, rename="camel", omit_defaults=True):
    """OCFL inventory structure.

    Field names use snake_case in Python but serialize to camelCase JSON
    (e.g. digest_algorithm ↔ digestAlgorithm).

    Mutation contract: this struct (and VersionInfo, UserInfo) is frozen, so
    field reassignment raises AttributeError. msgspec frozen=True does NOT
    block mutation of contained dict/list values (manifest, versions, state,
    fixity); mutating those in place is undefined behavior. Build new
    inventories via InventoryBuilder.
    """

    id: str
    type: str
    digest_algorithm: str
    head: str
    manifest: dict[str, list[str]]
    versions: dict[str, VersionInfo]
    content_directory: str | None = None
    fixity: dict[str, dict[str, list[str]]] | None = None


def _sort_path_lists(obj: dict) -> None:
    """Sort all path lists in manifest, version states, and fixity in place."""
    if "manifest" in obj:
        for digest in obj["manifest"]:
            obj["manifest"][digest].sort()
    if "versions" in obj:
        for ver in obj["versions"].values():
            if "state" in ver:
                for digest in ver["state"]:
                    ver["state"][digest].sort()
    if obj.get("fixity"):
        for alg in obj["fixity"].values():
            for digest in alg:
                alg[digest].sort()


def _encode_inventory(inv: Inventory) -> bytes:
    """Canonical compact JSON encoding for an Inventory."""
    obj = msgspec.to_builtins(inv)
    _sort_path_lists(obj)
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


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
        """Encode inventory to canonical compact JSON bytes."""
        raw = _encode_inventory(inv)
        digest = bytes_digest(raw, alg)
        return cls(inventory=inv, raw_bytes=raw, digest=digest, digest_algorithm=alg)

    @property
    def sidecar_filename(self) -> str:
        return f"inventory.json.{self.digest_algorithm.ocfl_name}"

    @property
    def sidecar_content(self) -> str:
        return f"{self.digest} inventory.json\n"
