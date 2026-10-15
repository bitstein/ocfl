import pytest

from ocfl_lib._layout._component import require_disk_safe_component
from ocfl_lib.validation._limits import MAX_OBJECT_ID


@pytest.mark.parametrize("s", ["abc", "urn-nbn-de", "a", "x" * MAX_OBJECT_ID])
def test_require_disk_safe_component_happy(s):
    require_disk_safe_component(s)


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
def test_require_disk_safe_component_rejects(s):
    with pytest.raises(ValueError):
        require_disk_safe_component(s)


def test_require_disk_safe_component_what_in_message():
    with pytest.raises(ValueError, match="widget id"):
        require_disk_safe_component("..", what="widget id")
