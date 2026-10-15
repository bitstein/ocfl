from datetime import UTC
from typing import TYPE_CHECKING, Self

from ocfl_lib._digest import DigestAlgorithm
from ocfl_lib._errors import InvalidPath, InventoryBuildError
from ocfl_lib._inventory import Inventory, UserInfo, VersionInfo
from ocfl_lib._spec import (
    DEFAULT_CONTENT_DIRECTORY,
    DEFAULT_SPEC,
    Spec,
    VNum,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ocfl_lib._digest_map import DigestMap
    from ocfl_lib._sources import FixitySource


class InventoryBuilder:
    """Build an Inventory, optionally one that extends `prev` by new versions.

    `prev` seeds the id, spec, content directory, digest algorithm,
    manifest, versions and fixity; each `add_version` becomes the next
    version after its head (v1 without `prev`). A digest new to the
    manifest gets one content path, `<version>/<content dir>/<its first
    logical path>`, mapped by `content_path_func` when one is set. A
    builder is single-use. `build` raises
    InventoryBuildError when called twice, when no id is set, when no
    version was added, or when a state digest has no logical paths, and
    InvalidPath for a bad logical path or content directory.
    """

    def __init__(self, prev: Inventory | None = None):
        self._prev = prev
        self._id: str | None = prev.id if prev else None
        self._spec: Spec = Spec.from_inventory_type(prev.type) if prev else DEFAULT_SPEC
        self._content_dir: str | None = prev.content_directory if prev else None
        self._digest_alg: DigestAlgorithm = (
            DigestAlgorithm.from_ocfl_name(prev.digest_algorithm)
            if prev
            else DigestAlgorithm.SHA512
        )
        self._content_path_func: Callable[[str], str] | None = None
        self._fixity_source: FixitySource | None = None
        self._pending_versions: list[
            tuple[DigestMap, str, str | None, UserInfo | None]
        ] = []
        self._built = False

    def id(self, object_id: str) -> Self:
        self._id = object_id
        return self

    def spec(self, spec: Spec) -> Self:
        self._spec = spec
        return self

    def content_directory(self, cd: str) -> Self:
        self._content_dir = cd
        return self

    def digest_algorithm(self, alg: DigestAlgorithm) -> Self:
        self._digest_alg = alg
        return self

    def fixity_source(self, fs: FixitySource | None) -> Self:
        self._fixity_source = fs
        return self

    def content_path_func(self, fn: Callable[[str], str] | None) -> Self:
        self._content_path_func = fn
        return self

    def add_version(
        self,
        state: DigestMap,
        *,
        created: str | None = None,
        message: str | None = None,
        user: UserInfo | None = None,
    ) -> Self:
        if created is None:
            from datetime import datetime

            created = datetime.now(UTC).isoformat()
        self._pending_versions.append((state, created, message, user))
        return self

    def build(self) -> Inventory:
        if self._built:
            raise InventoryBuildError("build() already called; builders are single-use")
        self._built = True
        if not self._id:
            raise InventoryBuildError("id must be set")
        if not self._pending_versions:
            raise InventoryBuildError("at least one version must be added")

        if self._content_dir is None:
            content_dir = DEFAULT_CONTENT_DIRECTORY
        else:
            _validate_content_directory(self._content_dir)
            content_dir = self._content_dir
        prev = self._prev

        if prev:
            manifest: dict[str, list[str]] = {
                d: list(ps) for d, ps in prev.manifest.items()
            }
            versions: dict[str, VersionInfo] = dict(prev.versions)
            next_vnum = VNum.parse(prev.head).next()
        else:
            manifest = {}
            versions = {}
            next_vnum = VNum.first()

        existing_digests = {d.lower() for d in manifest}

        fixity: dict[str, dict[str, list[str]]] = {}
        if prev and prev.fixity:
            for alg_name, dm in prev.fixity.items():
                fixity[alg_name] = {d: list(ps) for d, ps in dm.items()}

        head_vnum = VNum.parse(prev.head) if prev else VNum.first()

        for state, created, message, user in self._pending_versions:
            for digest, logical_paths in state.items():
                if not logical_paths:
                    raise InventoryBuildError(
                        f"state digest {digest!r} must include at least one "
                        "logical path"
                    )
                for logical_path in logical_paths:
                    _validate_relative_path(logical_path, kind="logical path")

            vnum = next_vnum
            head_vnum = vnum
            vnum_str = str(vnum)

            for digest, logical_paths in sorted(state.items()):
                if digest.lower() not in existing_digests:
                    logical = logical_paths[0]
                    if self._content_path_func:
                        logical = self._content_path_func(logical)
                    content_path = f"{vnum_str}/{content_dir}/{logical}"
                    manifest[digest] = [content_path]
                    existing_digests.add(digest.lower())

                    if self._fixity_source is not None:
                        fixity_digests = self._fixity_source.get_fixity(digest)
                        for fix_alg, fix_digest in fixity_digests.items():
                            alg_name = fix_alg.ocfl_name
                            fixity.setdefault(alg_name, {}).setdefault(
                                fix_digest, []
                            ).append(content_path)

            versions[vnum_str] = VersionInfo(
                created=created,
                state=state,
                message=message,
                user=user,
            )

            next_vnum = vnum.next()

        return Inventory(
            id=self._id,
            type=self._spec.inventory_type_url,
            digest_algorithm=self._digest_alg.ocfl_name,
            head=str(head_vnum),
            manifest=manifest,
            versions=versions,
            content_directory=self._content_dir,
            fixity=fixity or None,
        )


def _validate_relative_path(path: str, *, kind: str) -> None:
    if not path or path.startswith("/") or path.endswith("/"):
        raise InvalidPath(f"invalid {kind}: {path!r}")
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise InvalidPath(f"invalid {kind}: {path!r}")


def _validate_content_directory(content_dir: str) -> None:
    if not content_dir or "/" in content_dir or content_dir in (".", ".."):
        raise InvalidPath(f"invalid content directory: {content_dir!r}")
