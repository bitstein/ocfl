import posixpath
from typing import TYPE_CHECKING

from ocfl_lib._commit import commit as commit_plan
from ocfl_lib._commit_plan import CommitPlan, build_plan
from ocfl_lib._errors import (
    ContentNotFound,
    LayoutNotConfigured,
    MultipleRootDeclarations,
    NotAnOcflRoot,
    PlanMismatchError,
    RootExistsError,
    SpecVersionError,
)
from ocfl_lib._layout import DEFAULT_LAYOUTS, get_layout
from ocfl_lib._layout.config import (
    read_layout_config,
    read_ocfl_layout,
    write_layout_config,
    write_ocfl_layout,
)
from ocfl_lib._object import Object
from ocfl_lib._path import join_path
from ocfl_lib._purge import purge
from ocfl_lib._recovery import CommitStatus, RecoveryAction, detect, recover
from ocfl_lib._spec import DEFAULT_SPEC, Namaste, Spec
from ocfl_lib.fs import _utils as fs_utils

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ocfl_lib._inventory import UserInfo
    from ocfl_lib._layout.registry import LayoutRegistry
    from ocfl_lib._layout.types import Layout
    from ocfl_lib._sources import ContentSource
    from ocfl_lib._version_state import VersionState
    from ocfl_lib.fs._protocols import FS, FileWalker, WriteFS


class StorageRoot:
    """Interface to an OCFL storage root.

    Requires a FileWalker FS (walking is fundamental to root operations).
    The constructor does no I/O; `open` reads an existing root's spec and
    layout (fail fast on bad config).
    """

    def __init__(
        self,
        fsys: FileWalker,
        path: str = "",
        *,
        spec: Spec = DEFAULT_SPEC,
        layout: Layout | None = None,
    ) -> None:
        self._fsys = fsys
        self._path = path
        self._spec = spec
        self._layout = layout

    @classmethod
    def open(
        cls,
        fsys: FileWalker,
        path: str = "",
        *,
        layouts: LayoutRegistry = DEFAULT_LAYOUTS,
    ) -> StorageRoot:
        """Open an existing storage root.

        Raises NotAnOcflRoot / MultipleRootDeclarations on a bad declaration
        and UnknownLayout if the declared layout is not in `layouts`.
        """
        spec = _detect_spec(fsys, path)
        layout = _load_layout(fsys, path, layouts)
        return cls(fsys, path, spec=spec, layout=layout)

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

        return cls(walker, path, spec=spec, layout=layout)

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

        Refuses while a commit of the object is live (ObjectLockedError) or
        unfinished or stray (RecoveryRequiredError). Deletes in the ADR-0001
        order: root inventory, root sidecar, version dirs highest first,
        `extensions/` and `logs/`, the NAMASTE, then empty parents. Every
        partial state is still an object or gone, and a re-run finishes it.

        Raises ObjectNotFound if missing, LayoutNotConfigured if no layout.
        """
        obj_path = join_path(self._path, self.object_path(object_id))
        purge(fsys, self._path, obj_path, object_id)

    def plan(
        self,
        id: str,
        version_state: VersionState,
        *,
        spec: Spec | None = None,
        message: str | None = None,
        user: UserInfo | None = None,
        created: str | None = None,
    ) -> CommitPlan:
        """Plan the next version of object `id`. Reads only.

        For a new object, spec defaults to the root's spec.
        Raises LayoutNotConfigured if no layout is configured.
        """
        obj = self.get_object(id)
        obj_spec = spec if spec is not None else (None if obj.exists else self._spec)
        effective = obj_spec if obj_spec is not None else obj.spec
        if effective.is_newer_than(self._spec):
            raise SpecVersionError(
                f"E081: object spec {effective} is newer than "
                f"storage root spec {self._spec}"
            )
        return build_plan(
            obj,
            version_state,
            object_id=id,
            spec=obj_spec,
            message=message,
            user=user,
            created=created,
        )

    def commit(
        self,
        fsys: WriteFS,
        id: str,
        version: VersionState | CommitPlan,
        *,
        content: ContentSource | None = None,
        spec: Spec | None = None,
        message: str | None = None,
        user: UserInfo | None = None,
        created: str | None = None,
    ) -> Object:
        """Write the next version of object `id`. The only way to write one.

        `version` is a VersionState (planned here, with spec, message, user
        and created) or a CommitPlan from `plan`, whose content comes from
        `content`. Raises StaleHeadError if the object moved past the plan's
        base. Returns the object at its new head.
        """
        if isinstance(version, CommitPlan):
            if any(v is not None for v in (spec, message, user, created)):
                raise PlanMismatchError(
                    "spec, message, user and created belong to plan(), not to "
                    "a commit of a CommitPlan"
                )
            plan = version
            if plan.object_id != id:
                raise PlanMismatchError(
                    f"plan is for object {plan.object_id!r}, not {id!r}"
                )
        else:
            plan = self.plan(
                id, version, spec=spec, message=message, user=user, created=created
            )
            if content is None:
                content = version.content_source
        obj = self.get_object(id)
        commit_plan(fsys, self._path, obj.path, plan, content or _NO_CONTENT)
        return Object(self._fsys, obj.path)

    def commit_status(self, id: str) -> CommitStatus:
        """How far the last commit of object `id` got. Reads only."""
        obj_path = join_path(self._path, self.object_path(id))
        status, _plan = detect(self._fsys, self._path, obj_path, id)
        return status

    def recover(
        self,
        fsys: WriteFS,
        id: str,
        action: RecoveryAction,
        *,
        version: str | None = None,
        content: ContentSource | None = None,
    ) -> CommitStatus:
        """Finish or undo the last commit of object `id`. Returns the state found.

        RESUME finishes a claimed version (an unclaimed one is discarded;
        nothing of it is in the object). REVERT undoes a version that is not
        published yet, else raises RevertCompletedError. REMOVE_STRAY removes
        the stray claim `version` and its inventory proof. RESUME or REVERT
        discards an unreadable plan.json (state UNREADABLE_PLAN); check the
        status again afterwards. Every action also
        removes empty work dirs and empty layout parents (E073). On S3,
        resuming a claim whose content is incomplete needs `content`.
        """
        obj_path = join_path(self._path, self.object_path(id))
        return recover(
            fsys, self._path, obj_path, id, action, version=version, content=content
        )

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


class _NoContent:
    """ContentSource for a plan committed without one."""

    def get_content(self, digest: str) -> tuple[FS, str]:
        raise ContentNotFound(f"no content source for {digest}")


_NO_CONTENT = _NoContent()


def _detect_spec(fsys: FileWalker, path: str) -> Spec:
    """Find and parse the root NAMASTE declaration."""
    try:
        entries = list(fsys.read_dir(path))
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


def _load_layout(fsys: FileWalker, path: str, layouts: LayoutRegistry) -> Layout | None:
    """Load the declared layout with its extension config, or None."""
    info = read_ocfl_layout(fsys, root=path)
    if info is None:
        return None
    ext_name, _description = info
    config = read_layout_config(fsys, ext_name, root=path)
    return get_layout(ext_name, config=config, layouts=layouts)
