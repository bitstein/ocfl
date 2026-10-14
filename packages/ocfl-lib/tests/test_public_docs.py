from pathlib import Path

import ocfl_lib
from tests.helpers.docs import documented_members, undocumented_exports

DOCUMENTED_API = Path(__file__).with_name("documented_api.txt")


def test_package_has_a_docstring() -> None:
    assert (ocfl_lib.__doc__ or "").strip()


def test_every_export_has_its_own_docstring() -> None:
    assert undocumented_exports(ocfl_lib) == []


def test_documented_members_keep_their_docstrings() -> None:
    kept = set(DOCUMENTED_API.read_text().split())
    lost = sorted(kept - documented_members(ocfl_lib))
    assert lost == [], (
        f"public docstrings removed: {lost}; "
        f"if deliberate, drop them from {DOCUMENTED_API.name}"
    )
