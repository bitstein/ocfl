"""OCFL storage root operations."""

import posixpath
from typing import TYPE_CHECKING

from ocfl_lib._path import join_path
from ocfl_lib.errors import (
    LayoutNotConfigured,
    MultipleRootDeclarations,
    NotAnOcflRoot,
    ObjectNotFound,
    RootExistsError,
    SpecVersionError,
)
from ocfl_lib.fs import utils as fs_utils
from ocfl_lib.layout import DEFAULT_LAYOUTS, get_layout
from ocfl_lib.layout.config import (
    read_layout_config,
    read_ocfl_layout,
    write_layout_config,
    write_ocfl_layout,
)
from ocfl_lib.object import Object
from ocfl_lib.spec import Namaste, Spec

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ocfl_lib.fs.protocols import FileWalker, WriteFS
    from ocfl_lib.inventory import UserInfo
    from ocfl_lib.layout.registry import LayoutRegistry
    from ocfl_lib.layout.types import Layout
    from ocfl_lib.stage import Stage


class StorageRoot:
    """Interface to an OCFL storage root.

    Requires a FileWalker FS (walking is fundamental to root operations).
    Layout is loaded eagerly in the constructor (fail fast on bad config).
    """

    def __init__(
        self,
        fsys: FileWalker,
        path: str = "",
        *,
        layouts: LayoutRegistry = DEFAULT_LAYOUTS,
    ) -> None:
        self._fsys = fsys
        self._path = path
        self._layouts = layouts
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

        Writes NAMASTE declaration, the layout declaration (if layout), and
        extension config (if layout.config is not None).
        """
        for existing_spec in Spec:
            existing = join_path(path, Namaste.for_root(existing_spec).filename)
            if fsys.exists(existing):
                raise RootExistsError("OCFL storage root already initialized")

        namaste = Namaste.for_root(spec)
        namaste_path = join_path(path, namaste.filename)
        fs_utils.write_bytes(fsys, namaste_path, namaste.content.encode())

        if layout is not None:
            write_ocfl_layout(
                fsys,
                extension=layout.name,
                description=layout_description,
                root=path,
            )
            cfg = layout.config
            if cfg is not None:
                write_layout_config(fsys, layout.name, cfg, root=path)

        return cls(walker, path)

    @property
    def fs(self) -> FileWalker:
        return self._fsys

    @property
    def spec(self) -> Spec:
        return self._spec

    @property
    def layout(self) -> Layout | None:
        return self._layout

    def object_path(self, object_id: str) -> str:
        """Resolve an object ID to its storage path relative to root.

        Raises LayoutNotConfigured if no layout is configured.
        """
        if self._layout is None:
            raise LayoutNotConfigured
        return self._layout.resolve(object_id)

    def get_object(self, object_id: str) -> Object:
        """Get an Object by ID using the configured layout.

        Raises LayoutNotConfigured if no layout is configured.
        """
        obj_path = self.object_path(object_id)
        full_path = join_path(self._path, obj_path)
        return Object(self._fsys, full_path)

    def purge(self, fsys: WriteFS, object_id: str) -> None:
        """Permanently remove an object from the storage root.

        Resolves ID via layout, verifies object exists, removes object dir,
        and cleans empty parent directories up the hierarchy.

        NOT atomic. If ``remove_all`` fails partway through (permission
        error, network failure), the object directory may be left in a
        partially-deleted state with no automatic recovery. Treat purge
        failures as requiring manual investigation.

        Raises ObjectNotFound if missing, LayoutNotConfigured if no layout.
        """
        obj = self.get_object(object_id)
        if not obj.exists:
            raise ObjectNotFound(object_id)

        obj_path = self.object_path(object_id)
        full_path = join_path(self._path, obj_path)
        fsys.remove_all(full_path)

        # Clean empty parent directories up to (but not including) root
        parts = obj_path.split("/")
        for i in range(len(parts) - 1, 0, -1):
            parent = join_path(self._path, "/".join(parts[:i]))
            try:
                entries = list(self._fsys.read_dir(parent))
            except OSError:
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
        Raises LayoutNotConfigured if no layout is configured.
        """
        obj_path = self.object_path(id)
        full_path = join_path(self._path, obj_path)
        obj = Object(self._fsys, full_path)

        # Default spec to root's spec for new objects
        obj_spec = spec if spec is not None else (None if obj.exists else self._spec)
        effective = obj_spec if obj_spec is not None else obj.spec
        if effective.is_newer_than(self._spec):
            raise SpecVersionError(
                f"E081: object spec {effective} is newer than "
                f"storage root spec {self._spec}"
            )

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

    def object_paths(self, *, strict: bool = False) -> Iterator[str]:
        """Walk the hierarchy and yield paths to all OCFL objects.

        With ``strict=True``, ``OSError`` raised mid-walk propagates so the
        caller can distinguish a partial walk from a clean one. Default
        tolerates ``OSError`` for ad-hoc tooling that prefers a best-effort
        listing.
        """
        seen: set[str] = set()
        try:
            for ref in self._fsys.walk_files(self._path):
                parent, filename = posixpath.split(ref.path)
                n = Namaste.parse(filename)
                if n is not None and n.kind == "object" and parent not in seen:
                    seen.add(parent)
                    yield parent
        except OSError:
            if strict:
                raise
            return

    def objects(self) -> Iterator[Object]:
        """Walk the hierarchy and yield all OCFL Object instances."""
        for obj_path in self.object_paths():
            yield Object(self._fsys, obj_path)

    def objects_batch(self, *, max_workers: int | None = None) -> Iterator[Object]:
        """Load objects in parallel with bounded concurrency.

        Safe to abandon early; pool is cleaned up on consumer break.
        """
        from ocfl_lib._concurrent import bounded_map

        obj_paths = list(self.object_paths())
        if not obj_paths:
            return

        def _load(path: str) -> Object:
            obj = Object(self._fsys, path)
            # Force inventory load to parallelize I/O
            _ = obj.inventory
            return obj

        yield from bounded_map(_load, obj_paths, max_workers=max_workers)

    def _detect_spec(self) -> Spec:
        """Find and parse the root NAMASTE declaration."""
        try:
            entries = list(self._fsys.read_dir(self._path))
        except OSError as e:
            raise NotAnOcflRoot("no OCFL root declaration found") from e

        decls = []
        for entry in entries:
            n = Namaste.parse(entry.name)
            if n is not None and n.kind == "root":
                decls.append(n)

        if not decls:
            raise NotAnOcflRoot("no OCFL root declaration found")
        if len(decls) > 1:
            raise MultipleRootDeclarations([d.filename for d in decls])
        return decls[0].spec

    def _load_layout(self) -> Layout | None:
        """Load the declared layout with its extension config, or None."""
        info = read_ocfl_layout(self._fsys, root=self._path)
        if info is None:
            return None
        ext_name, _description = info
        config = read_layout_config(self._fsys, ext_name, root=self._path)
        return get_layout(ext_name, config=config, layouts=self._layouts)
