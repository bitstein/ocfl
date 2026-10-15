from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ocfl_lib.validation._report import FindingCollector

MAX_OBJECT_ID = 1024
MAX_PATH = 4096
MAX_MESSAGE = 65536
MAX_USER_NAME = 1024


def note_limits(
    result: FindingCollector, value: str, *, where: str, field: str, limit: int
) -> None:
    if "\x00" in value:
        result.info("L001", where=where, field=field)
    note_overlong(result, value, where=where, field=field, limit=limit)


def note_overlong(
    result: FindingCollector, value: str, *, where: str, field: str, limit: int
) -> None:
    if len(value) > limit:
        result.info("L002", where=where, field=field, limit=limit, actual=len(value))
