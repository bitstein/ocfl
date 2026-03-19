"""Tests for FS protocols, FileRef, and FsEntry."""

from io import BytesIO
from typing import TYPE_CHECKING, BinaryIO

if TYPE_CHECKING:
    from collections.abc import Iterator

from ocfl_lib.fs.protocols import (
    FS,
    CopyFS,
    FileRef,
    FileWalker,
    FsEntry,
    WriteFS,
)


class TestFileRef:
    def test_construction(self):
        ref = FileRef(path="v1/content/file.txt", size=42)
        assert ref.path == "v1/content/file.txt"
        assert ref.size == 42

    def test_frozen(self):
        ref = FileRef(path="a.txt", size=0)
        try:
            ref.path = "b.txt"  # type: ignore[misc]
            raise AssertionError("should be frozen")
        except AttributeError:
            pass

    def test_equality(self):
        a = FileRef(path="f.txt", size=10)
        b = FileRef(path="f.txt", size=10)
        assert a == b

    def test_inequality(self):
        a = FileRef(path="f.txt", size=10)
        b = FileRef(path="f.txt", size=20)
        assert a != b


class TestFsEntry:
    def test_construction(self):
        entry = FsEntry(name="subdir", is_dir=True, is_file=False, size=0)
        assert entry.name == "subdir"
        assert entry.is_dir is True
        assert entry.is_file is False
        assert entry.size == 0

    def test_file_entry(self):
        entry = FsEntry(name="data.bin", is_dir=False, is_file=True, size=1024)
        assert entry.is_file is True
        assert entry.is_dir is False
        assert entry.size == 1024

    def test_frozen(self):
        entry = FsEntry(name="x", is_dir=False, is_file=True, size=0)
        try:
            entry.name = "y"  # type: ignore[misc]
            raise AssertionError("should be frozen")
        except AttributeError:
            pass

    def test_equality(self):
        a = FsEntry(name="a", is_dir=True, is_file=False, size=0)
        b = FsEntry(name="a", is_dir=True, is_file=False, size=0)
        assert a == b


class TestProtocolsRuntimeCheckable:
    """Verify protocols are @runtime_checkable."""

    def test_fs_protocol(self):
        class MyFS:
            def open_file(self, name: str) -> BinaryIO:
                return BytesIO()

            def read_dir(self, name: str) -> Iterator[FsEntry]:
                yield from ()

        assert isinstance(MyFS(), FS)

    def test_write_fs_protocol(self):
        class MyWriteFS:
            def open_file(self, name: str) -> BinaryIO:
                return BytesIO()

            def read_dir(self, name: str) -> Iterator[FsEntry]:
                yield from ()

            def write(self, name: str, data: BinaryIO) -> int:
                return 0

            def remove(self, name: str) -> None:
                pass

            def remove_all(self, name: str) -> None:
                pass

            def mkdir(self, name: str) -> None:
                pass

        assert isinstance(MyWriteFS(), WriteFS)
        assert isinstance(MyWriteFS(), FS)

    def test_copy_fs_protocol(self):
        class MyCopyFS:
            def open_file(self, name: str) -> BinaryIO:
                return BytesIO()

            def read_dir(self, name: str) -> Iterator[FsEntry]:
                yield from ()

            def write(self, name: str, data: BinaryIO) -> int:
                return 0

            def remove(self, name: str) -> None:
                pass

            def remove_all(self, name: str) -> None:
                pass

            def mkdir(self, name: str) -> None:
                pass

            def copy(self, dst: str, src: str) -> int:
                return 0

        assert isinstance(MyCopyFS(), CopyFS)
        assert isinstance(MyCopyFS(), WriteFS)

    def test_file_walker_protocol(self):
        class MyWalker:
            def open_file(self, name: str) -> BinaryIO:
                return BytesIO()

            def read_dir(self, name: str) -> Iterator[FsEntry]:
                yield from ()

            def walk_files(self, dir: str) -> Iterator[FileRef]:
                yield from ()

        assert isinstance(MyWalker(), FileWalker)
        assert isinstance(MyWalker(), FS)

    def test_non_conforming_not_instance(self):
        class NotFS:
            pass

        assert not isinstance(NotFS(), FS)
        assert not isinstance(NotFS(), WriteFS)
        assert not isinstance(NotFS(), CopyFS)
        assert not isinstance(NotFS(), FileWalker)
