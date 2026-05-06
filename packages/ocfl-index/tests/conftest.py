"""Pytest fixtures for ocfl-index tests."""

from pathlib import Path

import pytest
from index_fixtures import FixtureRoot


@pytest.fixture
def fixture_root(tmp_path: Path) -> FixtureRoot:
    return FixtureRoot(tmp_path)
