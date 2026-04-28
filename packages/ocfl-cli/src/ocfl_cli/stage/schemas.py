"""Status DTOs and enums surfaced by the stage workflow."""

from enum import StrEnum
from typing import TypedDict


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


_NEXT_ACTION_HUMAN: dict[NextActionKind, str] = {
    NextActionKind.ADD_CONTENT: "stage add <path>",
    NextActionKind.TIMESTAMP_CONTENT: "stage timestamp content",
    NextActionKind.RETRY_CONTENT: "stage timestamp content --retry",
    NextActionKind.UPGRADE_CONTENT: (
        "stage timestamp content --upgrade (or --upgrade --watch)"
    ),
    NextActionKind.TIMESTAMP_INVENTORY: "stage timestamp inventory",
    NextActionKind.RETRY_INVENTORY: "stage timestamp inventory --retry",
    NextActionKind.UPGRADE_INVENTORY: (
        "stage timestamp inventory --upgrade (or --upgrade --watch)"
    ),
    NextActionKind.COMMIT: "stage commit",
    NextActionKind.NONE: "",
}


class _OutstandingJobJson(TypedDict):
    job_id: str
    digests: list[str]
    status: str
    submitted_at: str
    age_seconds: int
