"""OCFL storage root read operations."""

import posixpath
from typing import TYPE_CHECKING

from ocfl_lib.fs import utils as fs_utils
from ocfl_lib.layout import get_layout
from ocfl_lib.object import Object
from ocfl_lib.spec import Namaste, Spec

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ocfl_lib.fs.protocols import FileWalker
    from ocfl_lib.layout.types import Layout


class StorageRoot:
    """Read-only interface to an existing OCFL storage root.

    Requires a FileWalker FS (walking is fundamental to root operations).
    Layout is loaded eagerly in the constructor (fail fast on bad config).
    """

    def __init__(self, fsys: FileWalker, path: str = "") -> None:
        self._fsys = fsys
        self._path = path
        self._spec = self._detect_spec()
        self._layout = self._load_layout()

    @property
    def spec(self) -> Spec:
        return self._spec

    @property
    def layout(self) -> Layout | None:
        return self._layout

    def object_path(self, object_id: str) -> str:
        """Resolve an object ID to its storage path relative to root.

        Raises ValueError if no layout is configured.
        """
        if self._layout is None:
            raise ValueError("no layout configured")
        return self._layout.resolve(object_id)

    def get_object(self, object_id: str) -> Object:
        """Get an Object by ID using the configured layout.

        Raises ValueError if no layout is configured.
        """
        obj_path = self.object_path(object_id)
        full_path = _join(self._path, obj_path)
        return Object(self._fsys, full_path)

    def object_paths(self) -> Iterator[str]:
        """Walk the hierarchy and yield paths to all OCFL objects."""
        seen: set[str] = set()
        try:
            for ref in self._fsys.walk_files(self._path):
                parent, filename = posixpath.split(ref.path)
                n = Namaste.parse(filename)
                if n is not None and n.kind == "object" and parent not in seen:
                    seen.add(parent)
                    yield parent
        except FileNotFoundError, OSError:
            return

    def objects(self) -> Iterator[Object]:
        """Walk the hierarchy and yield all OCFL Object instances."""
        for obj_path in self.object_paths():
            yield Object(self._fsys, obj_path)

    def _detect_spec(self) -> Spec:
        """Find and parse the root NAMASTE declaration."""
        try:
            entries = list(self._fsys.read_dir(self._path))
        except (FileNotFoundError, OSError) as e:
            raise ValueError("no OCFL root declaration found") from e

        decls = []
        for entry in entries:
            n = Namaste.parse(entry.name)
            if n is not None and n.kind == "root":
                decls.append(n)

        if not decls:
            raise ValueError("no OCFL root declaration found")
        if len(decls) > 1:
            names = ", ".join(d.filename for d in decls)
            raise ValueError(f"multiple OCFL root declarations: {names}")
        return decls[0].spec

    def _load_layout(self) -> Layout | None:
        """Load layout from ocfl_layout.json + extension config, or None."""
        import msgspec

        layout_file = _join(self._path, "ocfl_layout.json")
        try:
            data = fs_utils.read_all(self._fsys, layout_file)
        except FileNotFoundError:
            return None
        layout_info = msgspec.json.decode(data)
        ext_name = layout_info["extension"]

        config_file = _join(self._path, f"extensions/{ext_name}/config.json")
        try:
            config_data = fs_utils.read_all(self._fsys, config_file)
            config = msgspec.json.decode(config_data)
        except FileNotFoundError:
            config = None
        return get_layout(ext_name, config=config)


def _join(base: str, name: str) -> str:
    """Join path components with '/' separator, handling empty base."""
    if not base:
        return name
    return f"{base}/{name}"
