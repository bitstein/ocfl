"""Tests for LocalFS backend."""

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from ocfl_lib.fs import CopyFS, FileWalker, WriteFS
from ocfl_lib.fs.local import LocalFS


@pytest.fixture
def fs_dir(tmp_path: Path) -> Path:
    """Create a temp dir with some files for testing."""
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("world")
    (tmp_path / "sub" / "deep").mkdir()
    (tmp_path / "sub" / "deep" / "c.txt").write_bytes(b"\x00\x01\x02")
    return tmp_path


@pytest.fixture
def fs(fs_dir: Path) -> LocalFS:
    return LocalFS(fs_dir)


class TestProtocolConformance:
    def test_is_write_fs(self, fs: LocalFS):
        assert isinstance(fs, WriteFS)

    def test_is_copy_fs(self, fs: LocalFS):
        assert isinstance(fs, CopyFS)

    def test_is_file_walker(self, fs: LocalFS):
        assert isinstance(fs, FileWalker)


class TestOpenFile:
    def test_read_file(self, fs: LocalFS):
        with fs.open_file("a.txt") as f:
            assert f.read() == b"hello"

    def test_read_nested(self, fs: LocalFS):
        with fs.open_file("sub/b.txt") as f:
            assert f.read() == b"world"

    def test_not_found(self, fs: LocalFS):
        with pytest.raises(FileNotFoundError):
            fs.open_file("nope.txt")


class TestReadDir:
    def test_root(self, fs: LocalFS):
        entries = sorted(fs.read_dir(""), key=lambda e: e.name)
        names = [e.name for e in entries]
        assert "a.txt" in names
        assert "sub" in names

    def test_subdir(self, fs: LocalFS):
        entries = sorted(fs.read_dir("sub"), key=lambda e: e.name)
        names = [e.name for e in entries]
        assert names == ["b.txt", "deep"]

    def test_entry_types(self, fs: LocalFS):
        entries = {e.name: e for e in fs.read_dir("")}
        assert entries["a.txt"].is_file is True
        assert entries["a.txt"].is_dir is False
        assert entries["sub"].is_dir is True
        assert entries["sub"].is_file is False
        assert entries["sub"].size is None

    def test_file_size(self, fs: LocalFS):
        entries = {e.name: e for e in fs.read_dir("")}
        assert entries["a.txt"].size == 5  # len("hello")

    def test_not_found(self, fs: LocalFS):
        with pytest.raises(FileNotFoundError):
            list(fs.read_dir("nope"))


class TestWrite:
    def test_write_new_file(self, fs: LocalFS, fs_dir: Path):
        from io import BytesIO

        n = fs.write("new.txt", BytesIO(b"content"))
        assert n == 7
        assert (fs_dir / "new.txt").read_bytes() == b"content"

    def test_write_overwrite(self, fs: LocalFS, fs_dir: Path):
        from io import BytesIO

        fs.write("a.txt", BytesIO(b"replaced"))
        assert (fs_dir / "a.txt").read_bytes() == b"replaced"

    def test_write_nested(self, fs: LocalFS, fs_dir: Path):
        from io import BytesIO

        fs.mkdir("newdir")
        n = fs.write("newdir/f.txt", BytesIO(b"data"))
        assert n == 4
        assert (fs_dir / "newdir" / "f.txt").read_bytes() == b"data"

    def test_write_atomic_no_partial_on_error(self, fs: LocalFS, fs_dir: Path):
        """If the source stream errors mid-read, no partial file should remain."""
        (fs_dir / "existing.txt").write_text("original")

        class BadStream:
            def read(self, n: int = -1) -> bytes:
                raise OSError("read failed")

        with pytest.raises(OSError, match="read failed"):
            fs.write("existing.txt", BadStream())  # type: ignore[arg-type]
        # Original file should be unchanged
        assert (fs_dir / "existing.txt").read_text() == "original"


class TestRemove:
    def test_remove_file(self, fs: LocalFS, fs_dir: Path):
        fs.remove("a.txt")
        assert not (fs_dir / "a.txt").exists()

    def test_remove_not_found(self, fs: LocalFS):
        with pytest.raises(FileNotFoundError):
            fs.remove("nope.txt")


class TestRemoveAll:
    def test_remove_dir(self, fs: LocalFS, fs_dir: Path):
        fs.remove_all("sub")
        assert not (fs_dir / "sub").exists()

    def test_remove_single_file(self, fs: LocalFS, fs_dir: Path):
        fs.remove_all("a.txt")
        assert not (fs_dir / "a.txt").exists()

    def test_remove_not_found(self, fs: LocalFS):
        with pytest.raises(FileNotFoundError):
            fs.remove_all("nope")


class TestMkdir:
    def test_create_dir(self, fs: LocalFS, fs_dir: Path):
        fs.mkdir("newdir")
        assert (fs_dir / "newdir").is_dir()

    def test_nested(self, fs: LocalFS, fs_dir: Path):
        fs.mkdir("a/b/c")
        assert (fs_dir / "a" / "b" / "c").is_dir()

    def test_existing_ok(self, fs: LocalFS):
        fs.mkdir("sub")  # already exists — should not raise


class TestCopy:
    def test_copy_file(self, fs: LocalFS, fs_dir: Path):
        n = fs.copy("a.txt", "copy.txt")
        assert n == 5
        assert (fs_dir / "copy.txt").read_bytes() == b"hello"
        # Source unchanged
        assert (fs_dir / "a.txt").read_bytes() == b"hello"

    def test_copy_nested(self, fs: LocalFS, fs_dir: Path):
        fs.mkdir("out")
        fs.copy("sub/b.txt", "out/b_copy.txt")
        assert (fs_dir / "out" / "b_copy.txt").read_bytes() == b"world"

    def test_copy_not_found(self, fs: LocalFS):
        with pytest.raises(FileNotFoundError):
            fs.copy("nope.txt", "dst.txt")


class TestWalkFiles:
    def test_walk_all(self, fs: LocalFS):
        refs = sorted(fs.walk_files(""), key=lambda r: r.path)
        paths = [r.path for r in refs]
        assert paths == ["a.txt", "sub/b.txt", "sub/deep/c.txt"]

    def test_walk_subdir(self, fs: LocalFS):
        refs = sorted(fs.walk_files("sub"), key=lambda r: r.path)
        paths = [r.path for r in refs]
        assert paths == ["sub/b.txt", "sub/deep/c.txt"]

    def test_sizes(self, fs: LocalFS):
        refs = {r.path: r for r in fs.walk_files("")}
        assert refs["a.txt"].size == 5
        assert refs["sub/b.txt"].size == 5
        assert refs["sub/deep/c.txt"].size == 3

    def test_empty_dir(self, fs_dir: Path):
        empty = fs_dir / "empty"
        empty.mkdir()
        local = LocalFS(empty)
        assert list(local.walk_files("")) == []


class TestPathValidation:
    def test_reject_absolute(self, fs: LocalFS):
        with pytest.raises(ValueError, match="absolute"):
            fs.open_file("/etc/passwd")

    def test_reject_dotdot(self, fs: LocalFS):
        with pytest.raises(ValueError, match="outside"):
            fs.open_file("../etc/passwd")

    def test_reject_dotdot_nested(self, fs: LocalFS):
        with pytest.raises(ValueError, match="outside"):
            fs.open_file("sub/../../etc/passwd")

    def test_reject_dotdot_write(self, fs: LocalFS):
        from io import BytesIO

        with pytest.raises(ValueError, match="outside"):
            fs.write("../escape.txt", BytesIO(b"bad"))

    def test_reject_dotdot_remove(self, fs: LocalFS):
        with pytest.raises(ValueError, match="outside"):
            fs.remove("../escape.txt")

    def test_reject_absolute_mkdir(self, fs: LocalFS):
        with pytest.raises(ValueError, match="absolute"):
            fs.mkdir("/tmp/evil")
