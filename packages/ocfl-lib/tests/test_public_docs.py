from pathlib import Path
from types import ModuleType

import pytest

import ocfl_lib
import ocfl_lib.fs
import ocfl_lib.validation
from tests.helpers.docs import documented_members, undocumented_exports

DOCUMENTED_API = Path(__file__).with_name("documented_api.txt")
PUBLIC_MODULES = [ocfl_lib, ocfl_lib.fs, ocfl_lib.validation]


@pytest.mark.parametrize("module", PUBLIC_MODULES, ids=lambda m: m.__name__)
def test_package_has_a_docstring(module: ModuleType) -> None:
    assert (module.__doc__ or "").strip()


@pytest.mark.parametrize("module", PUBLIC_MODULES, ids=lambda m: m.__name__)
def test_every_export_has_its_own_docstring(module: ModuleType) -> None:
    assert undocumented_exports(module) == []


def test_documented_members_keep_their_docstrings() -> None:
    kept = set(DOCUMENTED_API.read_text().split())
    found = set().union(*(documented_members(m) for m in PUBLIC_MODULES))
    lost = sorted(kept - found)
    assert lost == [], (
        f"public docstrings removed: {lost}; "
        f"if deliberate, drop them from {DOCUMENTED_API.name}"
    )
