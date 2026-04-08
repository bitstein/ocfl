"""Implementation-imposed sanity limits for archival-grade validation.

These are not spec-mandated. They refuse absurd input to prevent DoS and
ambiguous-intent hazards. Reuse from both non-fatal validation
(ValidationResult error codes L001/L002) and fail-fast write paths
(raising ValueError in StorageRoot.commit, stage, etc.).

Also declares filesystem-safety bounds beyond spec: ``validate_path_component``
defensively rejects object-id components that would be unsafe on disk.
"""

MAX_OBJECT_ID = 1024
MAX_PATH = 4096
MAX_MESSAGE = 65536
MAX_USER_NAME = 1024


def contains_null(s: str) -> bool:
    return "\x00" in s


def validate_path_component(s: str, *, what: str = "object id") -> None:
    """Defensively reject path components unsafe to write to disk.

    Stricter than the OCFL/extension specs. Raises plain ``ValueError``
    (not an ``OcflError`` subclass) because this is a filesystem-safety
    bound, not OCFL-semantic validation.
    """
    if s == "":
        raise ValueError(f"{what} must not be empty")
    if s == "." or s == "..":
        raise ValueError(f"{what} must not be {s!r}")
    if "/" in s or "\\" in s:
        raise ValueError(f"{what} must not contain a path separator")
    for ch in s:
        if ch == "\x7f" or ord(ch) < 0x20:
            raise ValueError(f"{what} must not contain control characters")
    if len(s) > MAX_OBJECT_ID:
        raise ValueError(f"{what} exceeds maximum length {MAX_OBJECT_ID}")
