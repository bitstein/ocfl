"""Status DTOs and enums surfaced by the stage workflow."""

from enum import StrEnum


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
