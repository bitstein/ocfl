"""Tests for FS utility functions."""

from typing import TYPE_CHECKING

import pytest

from ocfl_lib.fs.local import LocalFS
from ocfl_lib.fs.utils import copy, exists, read_all, read_text, write_bytes

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def fs_dir(tmp_path: Path) -> Path:
    (tmp_path / "hello.txt").write_text("hello world")
    (tmp_path / "binary.bin").write_bytes(b"\x00\x01\x02\x03")
    return tmp_path


@pytest.fixture
def fs(fs_dir: Path) -> LocalFS:
    return LocalFS(fs_dir)


class TestReadAll:
    def test_read_bytes(self, fs: LocalFS):
        data = read_all(fs, "binary.bin")
        assert data == b"\x00\x01\x02\x03"

    def test_read_text_file(self, fs: LocalFS):
        data = read_all(fs, "hello.txt")
        assert data == b"hello world"

    def test_not_found(self, fs: LocalFS):
        with pytest.raises(FileNotFoundError):
            read_all(fs, "nope.txt")


class TestReadText:
    def test_utf8(self, fs: LocalFS):
        assert read_text(fs, "hello.txt") == "hello world"

    def test_custom_encoding(self, fs_dir: Path):
        (fs_dir / "latin.txt").write_bytes("caf\xe9".encode("latin-1"))
        local = LocalFS(fs_dir)
        assert read_text(local, "latin.txt", encoding="latin-1") == "caf\xe9"

    def test_not_found(self, fs: LocalFS):
        with pytest.raises(FileNotFoundError):
            read_text(fs, "nope.txt")


class TestWriteBytes:
    def test_write_new(self, fs: LocalFS, fs_dir: Path):
        n = write_bytes(fs, "new.txt", b"new content")
        assert n == 11
        assert (fs_dir / "new.txt").read_bytes() == b"new content"

    def test_write_overwrite(self, fs: LocalFS, fs_dir: Path):
        write_bytes(fs, "hello.txt", b"replaced")
        assert (fs_dir / "hello.txt").read_bytes() == b"replaced"

    def test_write_empty(self, fs: LocalFS, fs_dir: Path):
        n = write_bytes(fs, "empty.txt", b"")
        assert n == 0
        assert (fs_dir / "empty.txt").read_bytes() == b""


class TestExists:
    def test_exists_true(self, fs: LocalFS):
        assert exists(fs, "hello.txt") is True

    def test_exists_false(self, fs: LocalFS):
        assert exists(fs, "nope.txt") is False


class TestCopy:
    def test_same_fs_uses_copy_fs(self, fs: LocalFS, fs_dir: Path):
        """When src and dst are the same CopyFS, use server-side copy."""
        n = copy(fs, "hello.txt", fs, "copy.txt")
        assert n == 11
        assert (fs_dir / "copy.txt").read_bytes() == b"hello world"

    def test_cross_fs(self, tmp_path: Path):
        """When src and dst are different FS instances, stream copy."""
        src_dir = tmp_path / "src"
        dst_dir = tmp_path / "dst"
        src_dir.mkdir()
        dst_dir.mkdir()
        (src_dir / "data.bin").write_bytes(b"cross copy")

        src_fs = LocalFS(src_dir)
        dst_fs = LocalFS(dst_dir)

        n = copy(src_fs, "data.bin", dst_fs, "data.bin")
        assert n == 10
        assert (dst_dir / "data.bin").read_bytes() == b"cross copy"

    def test_not_found(self, fs: LocalFS):
        with pytest.raises(FileNotFoundError):
            copy(fs, "nope.txt", fs, "dst.txt")
