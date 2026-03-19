"""OCFL validation error/warning codes."""

import json
from dataclasses import dataclass
from importlib.resources import files


@dataclass(frozen=True)
class ValidationCode:
    """A single validation code with its parameters and description template."""

    code: str
    params: list[str]
    description: str


def _load_codes() -> dict[str, ValidationCode]:
    data_file = files("ocfl_lib.data").joinpath("validation-errors.json")
    raw = json.loads(data_file.read_text(encoding="utf-8"))
    codes: dict[str, ValidationCode] = {}
    for code, entry in raw.items():
        codes[code] = ValidationCode(
            code=code,
            params=entry.get("params", []),
            description=entry["description"],
        )
    return codes


_CODES = _load_codes()


def get_code(code: str) -> ValidationCode:
    """Get a validation code by its identifier. Raises KeyError if unknown."""
    return _CODES[code]
