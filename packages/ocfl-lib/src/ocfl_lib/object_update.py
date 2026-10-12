"""Write a new version of an OCFL object.

`Object` only reads. These functions plan and apply a version update.
"""

from typing import TYPE_CHECKING

from ocfl_lib._path import join_path
from ocfl_lib.digest import DigestAlgorithm, stream_digest
from ocfl_lib.errors import SpecVersionError
from ocfl_lib.inventory import Inventory, StoredInventory, UserInfo, VersionInfo
from ocfl_lib.inventory_builder import InventoryBuilder
from ocfl_lib.object import Object
from ocfl_lib.spec import Spec
from ocfl_lib.update_plan import (
    RevertError,
    UpdatePlan,
    execute_plan,
    prepare_steps,
    revert_plan,
)

if TYPE_CHECKING:
    from ocfl_lib.fs.protocols import FS, WriteFS
    from ocfl_lib.sources import ContentSource
    from ocfl_lib.version_state import VersionState


def update_object(
    fsys: WriteFS,
    obj: Object,
    version_state: VersionState,
    *,
    id: str | None = None,
    spec: Spec | None = None,
    message: str | None = None,
    user: UserInfo | None = None,
    created: str | None = None,
) -> Object:
    """Create or update an OCFL object version.

    For new objects (not obj.exists), id is required.
    Builds inventory, creates update plan, executes it.
    On error: reverts completed steps, then re-raises.
    Returns a fresh Object for the new head.
    """
    plan = plan_update(
        obj,
        version_state,
        id=id,
        spec=spec,
        message=message,
        user=user,
        created=created,
    )
    prepare_steps(plan, fsys, obj.path, version_state.content_source)

    try:
        execute_plan(plan)
    except Exception as exc:
        try:
            revert_plan(plan)
        except RevertError as rev_exc:
            raise rev_exc from exc
        raise

    return Object(obj.fs, obj.path)


def plan_update(
    obj: Object,
    version_state: VersionState,
    *,
    id: str | None = None,
    spec: Spec | None = None,
    message: str | None = None,
    user: UserInfo | None = None,
    created: str | None = None,
) -> UpdatePlan:
    """Build an update plan without executing it.

    Does NOT embed ContentSource in the plan. Use apply_update_plan()
    to execute with a ContentSource.
    """
    old_stored = obj.inventory if obj.exists else None
    prev = old_stored.inventory if old_stored else None

    if prev is not None and spec is not None:
        prev_spec = Spec.from_inventory_type(prev.type)
        if prev_spec.is_newer_than(spec):
            raise SpecVersionError(
                f"E103: cannot downgrade object from spec {prev_spec} to {spec}"
            )

    expected_root_sidecar_digest = old_stored.digest if old_stored else None
    expected_root_sidecar_algorithm = (
        old_stored.digest_algorithm.ocfl_name if old_stored else ""
    )

    # Detect algorithm migration
    old_sidecar_alg = ""
    revert_stored = None
    if prev:
        prev_alg = DigestAlgorithm.from_ocfl_name(prev.digest_algorithm)
        if prev_alg != version_state.digest_algorithm:
            old_sidecar_alg = prev_alg.ocfl_name
            revert_stored = old_stored  # Preserve original for revert
            prev = _migrate_algorithm(
                prev, obj.fs, obj.path, version_state.digest_algorithm
            )
            old_stored = StoredInventory.from_inventory(
                prev, version_state.digest_algorithm
            )

    builder = InventoryBuilder(prev=prev)
    builder.digest_algorithm(version_state.digest_algorithm)
    if id is not None:
        builder.id(id)
    if spec is not None:
        builder.spec(spec)
    if version_state.fixity_source is not None:
        builder.fixity_source(version_state.fixity_source)

    builder.add_version(
        version_state.state,
        created=created,
        message=message,
        user=user,
    )
    new_inv = builder.build()

    alg = DigestAlgorithm.from_ocfl_name(new_inv.digest_algorithm)
    new_stored = StoredInventory.from_inventory(new_inv, alg)

    return UpdatePlan(
        steps=[],
        new_inv=new_stored,
        old_inv=old_stored,
        old_sidecar_alg=old_sidecar_alg,
        revert_inv=revert_stored,
        expected_root_sidecar_digest=expected_root_sidecar_digest,
        expected_root_sidecar_algorithm=expected_root_sidecar_algorithm,
    )


def apply_update_plan(
    fsys: WriteFS,
    obj: Object,
    plan: UpdatePlan,
    content_source: ContentSource,
) -> Object:
    """Execute a previously created update plan.

    Takes ContentSource separately (enables resume with different source).
    No auto-revert — caller handles errors and decides whether to revert.
    Returns a fresh Object for the new head.
    """
    prepare_steps(plan, fsys, obj.path, content_source)
    execute_plan(plan)
    return Object(obj.fs, obj.path)


def _migrate_algorithm(
    prev: Inventory,
    fsys: FS,
    obj_path: str,
    new_alg: DigestAlgorithm,
) -> Inventory:
    """Remap inventory digests from old algorithm to new.

    Re-digests all content files on disk, remaps manifest and version states,
    demotes old manifest to fixity[old_algorithm].
    """
    old_alg = DigestAlgorithm.from_ocfl_name(prev.digest_algorithm)

    # Re-digest each unique content file with new algorithm
    old_to_new: dict[str, str] = {}
    for old_digest, content_paths in prev.manifest.items():
        full_path = join_path(obj_path, content_paths[0])
        with fsys.open_file(full_path) as f:
            old_to_new[old_digest] = stream_digest(f, new_alg)

    # Rebuild manifest with new digest keys, same content paths
    new_manifest: dict[str, list[str]] = {}
    for old_digest, content_paths in prev.manifest.items():
        new_manifest[old_to_new[old_digest]] = list(content_paths)
    new_manifest = dict(sorted(new_manifest.items()))

    # Remap all version states
    new_versions: dict[str, VersionInfo] = {}
    for vkey, vinfo in prev.versions.items():
        new_state: dict[str, list[str]] = {}
        for old_digest, logical_paths in vinfo.state.items():
            nd = old_to_new[old_digest]
            if nd in new_state:
                new_state[nd].extend(logical_paths)
            else:
                new_state[nd] = list(logical_paths)
        new_versions[vkey] = VersionInfo(
            created=vinfo.created,
            state=dict(sorted(new_state.items())),
            message=vinfo.message,
            user=vinfo.user,
        )

    # Demote old manifest to fixity, preserve existing fixity
    fixity: dict[str, dict[str, list[str]]] = {}
    if prev.fixity:
        for alg_name, dm in prev.fixity.items():
            fixity[alg_name] = {d: list(ps) for d, ps in dm.items()}
    fixity[old_alg.ocfl_name] = {d: list(ps) for d, ps in prev.manifest.items()}

    return Inventory(
        id=prev.id,
        type=prev.type,
        digest_algorithm=new_alg.ocfl_name,
        head=prev.head,
        manifest=new_manifest,
        versions=new_versions,
        content_directory=prev.content_directory,
        fixity=fixity or None,
    )
