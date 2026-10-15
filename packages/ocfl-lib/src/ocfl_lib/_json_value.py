import json

type JsonValue = (
    bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
)
type JsonObject = dict[str, JsonValue]


def parse_json(data: bytes | str) -> JsonValue:
    return json.loads(data)


def as_object(value: JsonValue) -> JsonObject | None:
    return value if isinstance(value, dict) else None


def as_str(value: JsonValue) -> str | None:
    return value if isinstance(value, str) else None


def object_field_or_empty(obj: JsonObject, key: str) -> JsonObject:
    return as_object(obj.get(key)) or {}
