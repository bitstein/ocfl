import posixpath
from typing import TYPE_CHECKING, BinaryIO

from ocfl_lib._errors import OcflFileTypeError

if TYPE_CHECKING:
    from collections.abc import Collection, Iterator

    from ocfl_lib.fs._protocols import FileRef, FileWalker, FsEntry
    from ocfl_lib.validation._report import FindingCollector


class RegularEntries:
    """`fsys` listing only files and directories.

    Collects every other entry it meets in `unsupported`, path to kind.
    """

    def __init__(self, fsys: FileWalker) -> None:
        self.inner = fsys
        self.unsupported: dict[str, str] = {}

    def open_file(self, name: str) -> BinaryIO:
        return self.inner.open_file(name)

    def exists(self, name: str) -> bool:
        return self.inner.exists(name)

    def read_dir(self, name: str) -> Iterator[FsEntry]:
        try:
            yield from self.inner.read_dir(name)
        except OcflFileTypeError as e:
            self.unsupported.update(e.entries)

    def walk_files(self, name: str) -> Iterator[FileRef]:
        try:
            yield from self.inner.walk_files(name)
        except OcflFileTypeError as e:
            self.unsupported.update(e.entries)

    def report(
        self, result: FindingCollector, *, skip_dirs: Collection[str] = ()
    ) -> None:
        """E090 for each link, L003 for any other entry, except those in `skip_dirs`."""
        for path, kind in sorted(self.unsupported.items()):
            if posixpath.dirname(path) in skip_dirs:
                continue
            if kind == "symlink":
                result.error("E090", path=path)
            else:
                result.error("L003", path=path, kind=kind)
