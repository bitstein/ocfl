from ocfl_lib._errors import InvalidPath
from ocfl_lib.validation._limits import MAX_OBJECT_ID


def require_disk_safe_component(s: str, *, what: str = "object id") -> None:
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
