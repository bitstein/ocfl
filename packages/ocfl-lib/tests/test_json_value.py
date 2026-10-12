"""Tests for JsonValue narrowing helpers."""

import pytest

from ocfl_lib._json_value import (
    JsonObject,
    as_object,
    as_str,
    object_field,
    parse_json,
)


def test_parse_json():
    assert parse_json(b'{"a": [1, "x", null]}') == {"a": [1, "x", None]}
    with pytest.raises(ValueError):
        parse_json(b"{")


@pytest.mark.parametrize("value", [None, 1, 1.5, True, "s", [1]])
def test_as_object_rejects_non_objects(value):
    assert as_object(value) is None


def test_as_object_accepts_object():
    obj: JsonObject = {"k": 1}
    assert as_object(obj) is obj


@pytest.mark.parametrize("value", [None, 1, [], {}])
def test_as_str_rejects_non_strings(value):
    assert as_str(value) is None


def test_as_str_accepts_string():
    assert as_str("x") == "x"


def test_object_field():
    data: JsonObject = {"m": {"d": ["p"]}, "bad": [1]}
    assert object_field(data, "m") == {"d": ["p"]}
    assert object_field(data, "bad") == {}
    assert object_field(data, "missing") == {}
