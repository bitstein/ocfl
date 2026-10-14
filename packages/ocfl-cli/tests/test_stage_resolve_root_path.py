from pathlib import Path

import pytest

from ocfl_cli.stage._app import resolve_root_path


class TestResolveRootPath:
    def test_s3_uri_unchanged(self):
        assert resolve_root_path("s3://bucket/a/b") == "s3://bucket/a/b"

    def test_s3_uri_independent_of_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        keys = set()
        for sub in ("x", "y"):
            (tmp_path / sub).mkdir()
            monkeypatch.chdir(tmp_path / sub)
            keys.add(resolve_root_path("s3://bucket/root"))
        assert keys == {"s3://bucket/root"}

    def test_local_relative_path_resolved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.chdir(tmp_path)
        assert resolve_root_path("r") == str(tmp_path.resolve() / "r")

    def test_no_root_uses_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        assert resolve_root_path(None) == str(tmp_path.resolve())
