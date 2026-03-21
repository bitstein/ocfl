"""Tests for ocfl stage mv and ocfl stage rm."""


import pytest
from typer.testing import CliRunner

from ocfl_cli.main import app
from ocfl_cli.staging import StagingStore


@pytest.fixture
def runner():
    return CliRunner()


def _invoke(runner, storage_root, staging, *args):
    return runner.invoke(
        app,
        ["--root", str(storage_root), "stage", "--staging-root", str(staging), *args],
    )


def _setup_staged(runner, storage_root, tmp_path):
    """Create staging with two files: a.txt and dir/b.txt."""
    staging = tmp_path / "staging"
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "a.txt").write_text("aaa")
    sub = src_dir / "dir"
    sub.mkdir()
    (sub / "b.txt").write_text("bbb")
    (sub / "c.txt").write_text("ccc")

    _invoke(runner, storage_root, staging, "new", "obj:new")
    _invoke(runner, storage_root, staging, "add", "obj:new", str(src_dir))

    store = StagingStore(staging)
    root_path = str(storage_root.resolve())
    return staging, store, root_path


class TestStageMv:
    def test_mv_renames_path(self, runner, storage_root, tmp_path):
        staging, store, root_path = _setup_staged(runner, storage_root, tmp_path)
        result = _invoke(
            runner, storage_root, staging, "mv", "obj:new", "a.txt", "renamed.txt"
        )
        assert result.exit_code == 0, result.output

        inv = store.load_inventory(root_path, "obj:new")
        paths = [p for ps in inv.versions["v1"].state.values() for p in ps]
        assert "renamed.txt" in paths
        assert "a.txt" not in paths

    def test_mv_nonexistent_path_errors(self, runner, storage_root, tmp_path):
        staging, _store, _root_path = _setup_staged(runner, storage_root, tmp_path)
        result = _invoke(
            runner, storage_root, staging, "mv", "obj:new", "nope.txt", "dst.txt"
        )
        assert result.exit_code == 1


class TestStageRm:
    def test_rm_exact_path(self, runner, storage_root, tmp_path):
        staging, store, root_path = _setup_staged(runner, storage_root, tmp_path)
        result = _invoke(runner, storage_root, staging, "rm", "obj:new", "a.txt")
        assert result.exit_code == 0, result.output

        inv = store.load_inventory(root_path, "obj:new")
        paths = [p for ps in inv.versions["v1"].state.values() for p in ps]
        assert "a.txt" not in paths
        assert "dir/b.txt" in paths

    def test_rm_recursive(self, runner, storage_root, tmp_path):
        staging, store, root_path = _setup_staged(runner, storage_root, tmp_path)
        result = _invoke(runner, storage_root, staging, "rm", "obj:new", "dir", "-r")
        assert result.exit_code == 0, result.output

        inv = store.load_inventory(root_path, "obj:new")
        paths = [p for ps in inv.versions["v1"].state.values() for p in ps]
        assert "dir/b.txt" not in paths
        assert "dir/c.txt" not in paths
        assert "a.txt" in paths

    def test_rm_cleans_unreferenced_content(self, runner, storage_root, tmp_path):
        """Content.json entries for unreferenced digests are cleaned."""
        staging, store, root_path = _setup_staged(runner, storage_root, tmp_path)

        # Get digest of a.txt before removing
        inv = store.load_inventory(root_path, "obj:new")
        a_digest = None
        for d, ps in inv.versions["v1"].state.items():
            if "a.txt" in ps:
                a_digest = d
                break
        assert a_digest is not None

        cm_before = store.load_content_map(root_path, "obj:new")
        assert a_digest in cm_before

        _invoke(runner, storage_root, staging, "rm", "obj:new", "a.txt")

        cm_after = store.load_content_map(root_path, "obj:new")
        # Digest for a.txt should be cleaned from content map
        # (unless it's still referenced by another logical path)
        inv_after = store.load_inventory(root_path, "obj:new")
        all_digests = set(inv_after.versions[inv_after.head].state.keys())
        if a_digest not in all_digests:
            assert a_digest not in cm_after

    def test_rm_nonexistent_path_errors(self, runner, storage_root, tmp_path):
        staging, _store, _root_path = _setup_staged(runner, storage_root, tmp_path)
        result = _invoke(runner, storage_root, staging, "rm", "obj:new", "nope.txt")
        assert result.exit_code == 1
