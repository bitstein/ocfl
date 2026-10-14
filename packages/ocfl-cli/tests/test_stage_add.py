import pytest
from typer.testing import CliRunner

from ocfl_cli.main import app
from ocfl_cli.store.staging import StagingStore
from ocfl_lib import LocalFS, StorageRoot


@pytest.fixture
def runner():
    return CliRunner()


def _invoke_new(runner, storage_root, staging, object_id="obj:new"):
    return runner.invoke(
        app,
        [
            "--root",
            str(storage_root),
            "stage",
            "--staging-root",
            str(staging),
            "new",
            object_id,
        ],
    )


def _invoke_add(runner, storage_root, staging, object_id, *srcs, extra_args=None):
    args = [
        "--root",
        str(storage_root),
        "stage",
        "--staging-root",
        str(staging),
        "add",
        object_id,
        *[str(s) for s in srcs],
    ]
    if extra_args:
        args.extend(extra_args)
    return runner.invoke(app, args)


def _store_and_root(storage_root, staging):
    store = StagingStore(staging)
    root_path = str(storage_root.resolve())
    return store, root_path


class TestStageAdd:
    def test_add_single_file(self, runner, storage_root, tmp_path):
        staging = tmp_path / "staging"
        src = tmp_path / "hello.txt"
        src.write_text("hello world")

        _invoke_new(runner, storage_root, staging)
        result = _invoke_add(runner, storage_root, staging, "obj:new", src)
        assert result.exit_code == 0, result.output

        store, root_path = _store_and_root(storage_root, staging)
        inv = store.load_inventory(root_path, "obj:new")
        assert len(inv.versions["v1"].state) == 1
        paths = [p for ps in inv.versions["v1"].state.values() for p in ps]
        assert "hello.txt" in paths

        cm = store.load_content_map(root_path, "obj:new")
        assert len(cm) == 1
        entry = next(iter(cm.values()))
        assert entry["path"] == str(src)

    def test_add_nonexistent_source_fails_without_mutating_state(
        self, runner, storage_root, tmp_path
    ):
        staging = tmp_path / "staging"
        missing = tmp_path / "missing.txt"

        _invoke_new(runner, storage_root, staging)
        result = _invoke_add(runner, storage_root, staging, "obj:new", missing)

        assert result.exit_code == 1
        assert "source not found" in result.output.lower()
        store, root_path = _store_and_root(storage_root, staging)
        inv = store.load_inventory(root_path, "obj:new")
        assert inv.versions["v1"].state == {}

    def test_add_symlink_source_fails_without_mutating_state(
        self, runner, storage_root, tmp_path
    ):
        staging = tmp_path / "staging"
        _invoke_new(runner, storage_root, staging)

        target = tmp_path / "target.txt"
        target.write_text("data")
        source = tmp_path / "source.txt"
        source.symlink_to(target)

        result = _invoke_add(runner, storage_root, staging, "obj:new", source)

        assert result.exit_code == 1
        assert "unsupported source" in result.output.lower()
        store, root_path = _store_and_root(storage_root, staging)
        inv = store.load_inventory(root_path, "obj:new")
        assert inv.versions["v1"].state == {}

    def test_add_directory(self, runner, storage_root, tmp_path):
        staging = tmp_path / "staging"
        src_dir = tmp_path / "mydir"
        src_dir.mkdir()
        (src_dir / "a.txt").write_text("aaa")
        sub = src_dir / "sub"
        sub.mkdir()
        (sub / "b.txt").write_text("bbb")

        _invoke_new(runner, storage_root, staging)
        result = _invoke_add(runner, storage_root, staging, "obj:new", src_dir)
        assert result.exit_code == 0, result.output

        store, root_path = _store_and_root(storage_root, staging)
        inv = store.load_inventory(root_path, "obj:new")
        paths = sorted(p for ps in inv.versions["v1"].state.values() for p in ps)
        assert "a.txt" in paths
        assert "sub/b.txt" in paths

    def test_add_with_as(self, runner, storage_root, tmp_path):
        staging = tmp_path / "staging"
        src = tmp_path / "data.txt"
        src.write_text("data")

        _invoke_new(runner, storage_root, staging)
        result = _invoke_add(
            runner,
            storage_root,
            staging,
            "obj:new",
            src,
            extra_args=["--as", "docs/readme.txt"],
        )
        assert result.exit_code == 0, result.output

        store, root_path = _store_and_root(storage_root, staging)
        inv = store.load_inventory(root_path, "obj:new")
        paths = [p for ps in inv.versions["v1"].state.values() for p in ps]
        assert "docs/readme.txt" in paths

    @pytest.mark.parametrize(
        "logical_path",
        ["/absolute.txt", "../escape.txt", "a/../b.txt", "a//b.txt", "a/./b.txt"],
    )
    def test_add_rejects_invalid_logical_path(
        self, runner, storage_root, tmp_path, logical_path
    ):
        staging = tmp_path / "staging"
        src = tmp_path / "data.txt"
        src.write_text("data")

        _invoke_new(runner, storage_root, staging)
        result = _invoke_add(
            runner,
            storage_root,
            staging,
            "obj:new",
            src,
            extra_args=["--as", logical_path],
        )
        assert result.exit_code == 1
        assert "invalid logical path" in result.output.lower()

        store, root_path = _store_and_root(storage_root, staging)
        inv = store.load_inventory(root_path, "obj:new")
        assert inv.versions["v1"].state == {}

    def test_add_dir_with_as_prefix(self, runner, storage_root, tmp_path):
        staging = tmp_path / "staging"
        src_dir = tmp_path / "mydir"
        src_dir.mkdir()
        (src_dir / "x.txt").write_text("x")

        _invoke_new(runner, storage_root, staging)
        result = _invoke_add(
            runner,
            storage_root,
            staging,
            "obj:new",
            src_dir,
            extra_args=["--as", "prefix"],
        )
        assert result.exit_code == 0, result.output

        store, root_path = _store_and_root(storage_root, staging)
        inv = store.load_inventory(root_path, "obj:new")
        paths = [p for ps in inv.versions["v1"].state.values() for p in ps]
        assert "prefix/x.txt" in paths

    def test_overwrite_existing_path(self, runner, storage_root, tmp_path):
        staging = tmp_path / "staging"
        src1 = tmp_path / "v1.txt"
        src1.write_text("version 1")
        src2 = tmp_path / "v2.txt"
        src2.write_text("version 2")

        _invoke_new(runner, storage_root, staging)
        _invoke_add(
            runner,
            storage_root,
            staging,
            "obj:new",
            src1,
            extra_args=["--as", "file.txt"],
        )
        result = _invoke_add(
            runner,
            storage_root,
            staging,
            "obj:new",
            src2,
            extra_args=["--as", "file.txt"],
        )
        assert result.exit_code == 0, result.output

        store, root_path = _store_and_root(storage_root, staging)
        inv = store.load_inventory(root_path, "obj:new")
        paths = [p for ps in inv.versions["v1"].state.values() for p in ps]
        assert paths.count("file.txt") == 1

    def test_dedup_with_prior_manifest(self, runner, storage_root, tmp_path):
        staging = tmp_path / "staging"
        _invoke_new(runner, storage_root, staging, "obj:test")
        store, root_path = _store_and_root(storage_root, staging)

        obj = StorageRoot.open(LocalFS(storage_root)).get_object("obj:test")
        inv = obj.inventory.inventory
        [target_digest] = [
            d for d, ps in inv.versions[inv.head].state.items() if "file1.txt" in ps
        ]
        with obj.open_logical_path(obj.head, "file1.txt") as f:
            dup_file = tmp_path / "dup.txt"
            dup_file.write_bytes(f.read())

        result = _invoke_add(
            runner,
            storage_root,
            staging,
            "obj:test",
            dup_file,
            extra_args=["--as", "dup.txt"],
        )
        assert result.exit_code == 0, result.output
        assert target_digest not in store.load_content_map(root_path, "obj:test")

    def test_skip_symlinks(self, runner, storage_root, tmp_path):
        staging = tmp_path / "staging"
        src_dir = tmp_path / "mydir"
        src_dir.mkdir()
        real = src_dir / "real.txt"
        real.write_text("real")
        link = src_dir / "link.txt"
        link.symlink_to(real)

        _invoke_new(runner, storage_root, staging)
        result = _invoke_add(runner, storage_root, staging, "obj:new", src_dir)
        assert result.exit_code == 0, result.output

        store, root_path = _store_and_root(storage_root, staging)
        inv = store.load_inventory(root_path, "obj:new")
        paths = [p for ps in inv.versions["v1"].state.values() for p in ps]
        assert "real.txt" in paths
        assert "link.txt" not in paths

    def test_skip_empty_dirs(self, runner, storage_root, tmp_path):
        staging = tmp_path / "staging"
        src_dir = tmp_path / "mydir"
        src_dir.mkdir()
        (src_dir / "empty").mkdir()
        (src_dir / "file.txt").write_text("content")

        _invoke_new(runner, storage_root, staging)
        result = _invoke_add(runner, storage_root, staging, "obj:new", src_dir)
        assert result.exit_code == 0, result.output

        store, root_path = _store_and_root(storage_root, staging)
        inv = store.load_inventory(root_path, "obj:new")
        paths = [p for ps in inv.versions["v1"].state.values() for p in ps]
        assert "file.txt" in paths
        assert not any("empty" in p for p in paths)
