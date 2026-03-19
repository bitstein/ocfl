"""Builder for constructing OCFL Inventory instances."""

from datetime import UTC
from typing import TYPE_CHECKING, Self

from ocfl_lib.digest import DigestAlgorithm
from ocfl_lib.inventory import Inventory, UserInfo, VersionInfo
from ocfl_lib.spec import (
    DEFAULT_CONTENT_DIRECTORY,
    DEFAULT_SPEC,
    Spec,
    VNum,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ocfl_lib.digest_map import DigestMap


class InventoryBuilder:
    """Builder pattern for constructing Inventory instances."""

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
        self._pending_versions: list[
            tuple[DigestMap, str, str | None, UserInfo | None]
        ] = []

    def id(self, id: str) -> Self:
        self._id = id
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
        if not self._id:
            raise ValueError("id must be set")
        if not self._pending_versions:
            raise ValueError("at least one version must be added")

        content_dir = self._content_dir or DEFAULT_CONTENT_DIRECTORY
        prev = self._prev

        # Start from previous inventory data or empty
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

        # Build existing manifest path→digest for dedup
        existing_digests = set(manifest.keys())

        for state, created, message, user in self._pending_versions:
            vnum = next_vnum
            vnum_str = str(vnum)

            # Add new content to manifest
            for digest, logical_paths in state.items():
                if digest not in existing_digests:
                    # Use first logical path for content path
                    logical = logical_paths[0]
                    if self._content_path_func:
                        logical = self._content_path_func(logical)
                    content_path = f"{vnum_str}/{content_dir}/{logical}"
                    manifest[digest] = [content_path]
                    existing_digests.add(digest)

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
            head=str(VNum.parse(max(versions.keys()))),
            manifest=manifest,
            versions=versions,
            content_directory=self._content_dir,
            fixity=prev.fixity if prev and prev.fixity else None,
        )
