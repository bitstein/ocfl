"""Implementation-imposed sanity limits for archival-grade validation.

These are not spec-mandated. They refuse absurd input to prevent DoS and
ambiguous-intent hazards. Reuse from non-fatal validation
(validation info codes L001/L002).

Also declares filesystem-safety bounds beyond spec: ``validate_path_component``
defensively rejects object-id components that would be unsafe on disk.
"""

from typing import TYPE_CHECKING

from ocfl_lib._errors import InvalidPath

if TYPE_CHECKING:
    from ocfl_lib.validation._report import FindingCollector

MAX_OBJECT_ID = 1024
MAX_PATH = 4096
MAX_MESSAGE = 65536
MAX_USER_NAME = 1024


def note_limits(
    result: FindingCollector, value: str, *, where: str, field: str, limit: int
) -> None:
    """L001 if `value` holds a null byte, then L002 if it is longer than `limit`."""
    if "\x00" in value:
        result.info("L001", where=where, field=field)
    note_overlong(result, value, where=where, field=field, limit=limit)


def note_overlong(
    result: FindingCollector, value: str, *, where: str, field: str, limit: int
) -> None:
    if len(value) > limit:
        result.info("L002", where=where, field=field, limit=limit, actual=len(value))


def validate_path_component(s: str, *, what: str = "object id") -> None:
    """Reject path components unsafe to write to disk.

    A filesystem-safety bound, stricter than the OCFL/extension specs.
    """
    if s == "":
        raise InvalidPath(f"{what} must not be empty")
    if s == "." or s == "..":
        raise InvalidPath(f"{what} must not be {s!r}")
    if "/" in s or "\\" in s:
        raise InvalidPath(f"{what} must not contain a path separator")
    for ch in s:
        if ch == "\x7f" or ord(ch) < 0x20:
            raise InvalidPath(f"{what} must not contain control characters")
    if len(s) > MAX_OBJECT_ID:
        raise InvalidPath(f"{what} exceeds maximum length {MAX_OBJECT_ID}")
