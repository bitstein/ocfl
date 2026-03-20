"""OCFL storage root operations."""

import posixpath
from typing import TYPE_CHECKING

import msgspec

from ocfl_lib.fs import utils as fs_utils
from ocfl_lib.layout import get_layout
from ocfl_lib.object import Object
from ocfl_lib.spec import Namaste, Spec

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ocfl_lib.fs.protocols import FileWalker, WriteFS
    from ocfl_lib.inventory import UserInfo
    from ocfl_lib.layout.types import Layout
    from ocfl_lib.stage import Stage


class StorageRoot:
    """Interface to an OCFL storage root.

    Requires a FileWalker FS (walking is fundamental to root operations).
    Layout is loaded eagerly in the constructor (fail fast on bad config).
    """

    def __init__(self, fsys: FileWalker, path: str = "") -> None:
        self._fsys = fsys
        self._path = path
        self._spec = self._detect_spec()
        self._layout = self._load_layout()

    @classmethod
    def init(
        cls,
        fsys: WriteFS,
        walker: FileWalker,
        path: str = "",
        *,
        spec: Spec = Spec.V1_1,
        layout: Layout | None = None,
        layout_description: str = "",
    ) -> StorageRoot:
        """Create a new OCFL storage root on disk.

        Writes NAMASTE declaration, ocfl_layout.json (if layout), and
        extension config (if layout.config is not None).
        """
        namaste = Namaste.for_root(spec)
        namaste_path = _join(path, namaste.filename)
        fs_utils.write_bytes(fsys, namaste_path, namaste.content.encode())

        if layout is not None:
            ocfl_layout = {"extension": layout.name, "description": layout_description}
            fs_utils.write_bytes(
                fsys,
                _join(path, "ocfl_layout.json"),
                msgspec.json.encode(ocfl_layout),
            )
            cfg = layout.config
            if cfg is not None:
                ext_dir = _join(path, f"extensions/{layout.name}")
                fsys.mkdir(ext_dir)
                fs_utils.write_bytes(
                    fsys,
                    _join(ext_dir, "config.json"),
                    msgspec.json.encode(cfg),
                )

        return cls(walker, path)

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

    def purge(self, fsys: WriteFS, object_id: str) -> None:
        """Permanently remove an object from the storage root.

        Resolves ID via layout, verifies object exists, removes object dir,
        and cleans empty parent directories up the hierarchy.
        Raises ValueError if object not found or no layout configured.
        """
        obj_path = self.object_path(object_id)
        full_path = _join(self._path, obj_path)
        obj = Object(self._fsys, full_path)
        if not obj.exists:
            raise ValueError(f"object {object_id!r} not found")

        fsys.remove_all(full_path)

        # Clean empty parent directories up to (but not including) root
        parts = obj_path.split("/")
        for i in range(len(parts) - 1, 0, -1):
            parent = _join(self._path, "/".join(parts[:i]))
            try:
                entries = list(self._fsys.read_dir(parent))
            except FileNotFoundError, OSError:
                break
            if entries:
                break
            fsys.remove_all(parent)

    def commit(
        self,
        fsys: WriteFS,
        id: str,
        stage: Stage,
        *,
        spec: Spec | None = None,
        message: str | None = None,
        user: UserInfo | None = None,
        created: str | None = None,
    ) -> Object:
        """Add or update an object in the storage root.

        Resolves id to path via layout, then delegates to Object.update().
        For new objects, spec defaults to root's spec.
        Raises ValueError if no layout is configured.
        """
        obj_path = self.object_path(id)
        full_path = _join(self._path, obj_path)
        obj = Object(self._fsys, full_path)

        # Default spec to root's spec for new objects
        obj_spec = spec if spec is not None else (None if obj.exists else self._spec)

        obj.update(
            fsys,
            stage,
            id=id,
            spec=obj_spec,
            message=message,
            user=user,
            created=created,
        )
        return obj

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

    def objects_batch(self, *, max_workers: int | None = None) -> Iterator[Object]:
        """Load objects in parallel using ThreadPoolExecutor."""
        from concurrent.futures import ThreadPoolExecutor

        obj_paths = list(self.object_paths())
        if not obj_paths:
            return

        def _load(path: str) -> Object:
            obj = Object(self._fsys, path)
            # Force inventory load to parallelize I/O
            _ = obj.inventory
            return obj

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            yield from pool.map(_load, obj_paths)

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
