"""Shared CLI test fixtures."""

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ocfl_lib import DigestAlgorithm, LocalFS, Spec, StorageRoot, get_layout, stage_dir


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path_factory, monkeypatch):
    """Point `OCFL_CONFIG` at nonexistent path; tests must not read real user config."""
    sentinel = tmp_path_factory.mktemp("ocfl-config") / "config.toml"
    monkeypatch.setenv("OCFL_CONFIG", str(sentinel))


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def fixtures_root() -> Path:
    root = Path(__file__).resolve().parents[3] / "tests" / "fixtures"
    assert root.is_dir()
    return root


@pytest.fixture
def storage_root(tmp_path: Path) -> Path:
    """Create a valid storage root with one object for testing."""
    fs = LocalFS(tmp_path)
    layout = get_layout("0002-flat-direct-storage-layout")
    root = StorageRoot.init(fs, fs, spec=Spec.V1_1, layout=layout)

    # Create a test object with 2 versions
    obj_dir = tmp_path / "test-files"
    obj_dir.mkdir()
    (obj_dir / "file1.txt").write_text("hello")
    stage = stage_dir(fs, "test-files", DigestAlgorithm.SHA512)
    root.commit(fs, "obj:test", stage, message="v1")

    # v2
    (obj_dir / "file2.txt").write_text("world")
    stage2 = stage_dir(fs, "test-files", DigestAlgorithm.SHA512)
    root.commit(fs, "obj:test", stage2, message="v2")

    shutil.rmtree(obj_dir)

    return tmp_path
