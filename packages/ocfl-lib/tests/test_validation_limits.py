import pytest

from ocfl_lib.validation._limits import MAX_OBJECT_ID, validate_path_component


@pytest.mark.parametrize("s", ["abc", "urn-nbn-de", "a", "x" * MAX_OBJECT_ID])
def test_validate_path_component_happy(s):
    validate_path_component(s)


@pytest.mark.parametrize(
    "s",
    [
        "",
        ".",
        "..",
        "a/b",
        "a\\b",
        "a\x00",
        "\x01",
        "\x7f",
        "x" * (MAX_OBJECT_ID + 1),
    ],
)
def test_validate_path_component_rejects(s):
    with pytest.raises(ValueError):
        validate_path_component(s)


def test_validate_path_component_what_in_message():
    with pytest.raises(ValueError, match="widget id"):
        validate_path_component("..", what="widget id")
