"""Test fixtures: build OCFL storage roots via ocfl_lib."""

from collections.abc import Mapping
from pathlib import Path

import pytest

from ocfl_lib import (
    DigestAlgorithm,
    LocalFS,
    StorageRoot,
    UserInfo,
    stage_dir,
)
from ocfl_lib.layout import FlatDirectLayout


def _write_files(dir_path: Path, files: Mapping[str, bytes | str]) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        target = dir_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            target.write_text(content)
        else:
            target.write_bytes(content)


class FixtureRoot:
    """Helper to build, mutate, and inspect a local OCFL storage root."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.base.mkdir(parents=True, exist_ok=True)
        self.root_dir = base / "root"
        self.root_dir.mkdir()
        self.fs = LocalFS(self.root_dir)
        self.root = StorageRoot.init(self.fs, self.fs, layout=FlatDirectLayout())
        self._counter = 0

    def commit(
        self,
        object_id: str,
        files: Mapping[str, bytes | str],
        *,
        algorithm: DigestAlgorithm = DigestAlgorithm.SHA256,
        message: str | None = None,
        user: UserInfo | None = None,
        created: str = "2024-01-01T00:00:00+00:00",
    ) -> None:
        self._counter += 1
        src_name = f"src-{self._counter}"
        src_dir = self.base / src_name
        _write_files(src_dir, files)

        stage = stage_dir(LocalFS(self.base), src_name, algorithm)
        self.root.commit(
            self.fs,
            object_id,
            stage,
            message=message,
            user=user,
            created=created,
        )

    def remove_object(self, object_id: str) -> None:
        self.root.purge(self.fs, object_id)

    @property
    def path(self) -> str:
        return str(self.root_dir)


@pytest.fixture
def fixture_root(tmp_path: Path) -> FixtureRoot:
    return FixtureRoot(tmp_path)
