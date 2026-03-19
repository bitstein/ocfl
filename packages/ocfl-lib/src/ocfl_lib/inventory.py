"""OCFL inventory model using msgspec structs."""

import msgspec


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
