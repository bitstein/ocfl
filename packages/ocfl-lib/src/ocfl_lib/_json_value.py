"""Typed view of parsed JSON plus narrowing helpers.

Validators read untrusted inventories, so every value is a `JsonValue`
until a helper narrows it. Each helper returns None on a type mismatch.
"""

import json

type JsonValue = (
    bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
)
type JsonObject = dict[str, JsonValue]


def parse_json(data: bytes | str) -> JsonValue:
    """`json.loads` typed as JsonValue. Raises ValueError on bad JSON."""
    return json.loads(data)


def as_object(value: JsonValue) -> JsonObject | None:
    return value if isinstance(value, dict) else None


def as_str(value: JsonValue) -> str | None:
    return value if isinstance(value, str) else None


def object_field(obj: JsonObject, key: str) -> JsonObject:
    """`obj[key]` if it is an object, else an empty object."""
    return as_object(obj.get(key)) or {}
