"""Staged version metadata and the next-action kinds the workflow reports."""

from enum import StrEnum

import msgspec


class NextActionKind(StrEnum):
    """Stable enum surfaced as `next_action_kind` in `stage status --json`.

    Scripts MUST tolerate unknown values (forward-compat).
    """

    TIMESTAMP_CONTENT = "timestamp_content"
    UPGRADE_CONTENT = "upgrade_content"
    RETRY_CONTENT = "retry_content"
    TIMESTAMP_INVENTORY = "timestamp_inventory"
    UPGRADE_INVENTORY = "upgrade_inventory"
    RETRY_INVENTORY = "retry_inventory"
    COMMIT = "commit"
    ADD_CONTENT = "add_content"
    NONE = "none"


class StagedUser(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, omit_defaults=True
):
    """Mutable-before-commit version user metadata persisted in metadata.json."""

    name: str | None = None
    address: str | None = None


class StagedMetadata(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, omit_defaults=True
):
    """Mutable-before-commit version metadata persisted in metadata.json."""

    message: str | None = None
    user: StagedUser | None = None
