"""Implementation-imposed sanity limits for archival-grade validation.

These are not spec-mandated. They refuse absurd input to prevent DoS and
ambiguous-intent hazards. Reuse from both non-fatal validation
(ValidationResult error codes L001/L002) and fail-fast write paths
(raising ValueError in StorageRoot.commit, stage, etc.).
"""

MAX_OBJECT_ID = 1024
MAX_PATH = 4096
MAX_MESSAGE = 65536
MAX_USER_NAME = 1024


def contains_null(s: str) -> bool:
    return "\x00" in s
