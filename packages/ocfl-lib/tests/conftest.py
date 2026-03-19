from pathlib import Path

import pytest

FIXTURES_ROOT = Path(__file__).resolve().parents[3] / "tests" / "fixtures"


@pytest.fixture
def fixtures_root() -> Path:
    """Root of the OCFL official test fixtures."""
    assert FIXTURES_ROOT.is_dir(), f"Fixtures not found at {FIXTURES_ROOT}. Run: git submodule update --init"
    return FIXTURES_ROOT


@pytest.fixture
def good_objects_1_1(fixtures_root: Path) -> Path:
    return fixtures_root / "1.1" / "good-objects"


@pytest.fixture
def bad_objects_1_1(fixtures_root: Path) -> Path:
    return fixtures_root / "1.1" / "bad-objects"


@pytest.fixture
def warn_objects_1_1(fixtures_root: Path) -> Path:
    return fixtures_root / "1.1" / "warn-objects"


@pytest.fixture
def good_objects_1_0(fixtures_root: Path) -> Path:
    return fixtures_root / "1.0" / "good-objects"


@pytest.fixture
def bad_objects_1_0(fixtures_root: Path) -> Path:
    return fixtures_root / "1.0" / "bad-objects"


@pytest.fixture
def warn_objects_1_0(fixtures_root: Path) -> Path:
    return fixtures_root / "1.0" / "warn-objects"
