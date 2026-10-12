"""Commit use cases: build the plan from staging, commit it, purge staging."""

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

from ocfl_cli.errors import (
    AttestationMismatchError,
    CommitClaimLostError,
    CommitFailedError,
    CommitInterruptedError,
    LockTimeoutError,
    MissingStagedMetadataError,
    PhaseError,
    StagingFileChangedError,
    StagingFileMissingError,
)
from ocfl_cli.model.metadata import StagedMetadata, StagedUser
from ocfl_cli.model.phase import check_transition
from ocfl_cli.store.locks import object_commit_lock
from ocfl_cli.store.ots_state import (
    load_ots,
    ots_cache_path,
    plan_path,
    with_lock,
)
from ocfl_cli.store.staging import StagingStore
from ocfl_cli.workflow.common import (
    lock_timeout,
    log_phase,
    require_verified_content_before_inventory,
)
from ocfl_lib import (
    ClaimLostError,
    CommitPlan,
    ObjectLockedError,
    OtsParseError,
    PlanMismatchError,
    UserInfo,
    VersionInfo,
)
from ocfl_lib.ots import parse as parse_ots

if TYPE_CHECKING:
    from ocfl_lib import ContentSource, StorageRoot
    from ocfl_lib.fs import FS, WriteFS


class _StagingContentSource:
    """ContentSource resolving digests against the staging content map."""

    def __init__(self, cm: dict[str, dict]) -> None:
        self._cm = cm

    def get_content(self, digest: str) -> tuple[FS, str]:
        from ocfl_lib import LocalFS

        if digest in self._cm:
            file_path = Path(self._cm[digest]["path"])
            return LocalFS(file_path.parent), file_path.name
        raise KeyError(digest)


def _validate_and_build_content_source(
    store: StagingStore,
    root_path: str,
    object_id: str,
) -> _StagingContentSource:
    """Re-load content_map, validate stale-file size+mtime, build source."""
    content_map = store.load_content_map(root_path, object_id)
    for entry in content_map.values():
        file_path = Path(entry["path"])
        if not file_path.exists():
            raise StagingFileMissingError(f"source file missing: {file_path}")
        stat = file_path.stat()
        if stat.st_size != entry["size"] or stat.st_mtime != entry["mtime"]:
            raise StagingFileChangedError(
                f"source file changed since staging: {file_path}"
            )
    return _StagingContentSource(content_map)


def staged_content_source(
    store: StagingStore, root_path: str, object_id: str
) -> ContentSource:
    """Content source over the object's staged files (checked unchanged)."""
    return _validate_and_build_content_source(store, root_path, object_id)


def build_inventory_plan(
    store: StagingStore,
    sr,
    root_path: str,
    object_id: str,
    *,
    message: str | None = None,
    user: UserInfo | None = None,
    created: str | None = None,
) -> tuple[CommitPlan, ContentSource]:
    """Build a CommitPlan from the staged inventory + content map.

    Used by both `stage commit` (which commits it) and `stage timestamp
    inventory` (which saves it as `plan.json` for a later commit). Returns
    the plan and the `_StagingContentSource` the commit reads content from.
    """
    from ocfl_lib import DigestAlgorithm, VersionState

    inv = store.load_inventory(root_path, object_id)
    alg = DigestAlgorithm.from_ocfl_name(inv.digest_algorithm)
    head = inv.head

    content_source = _validate_and_build_content_source(store, root_path, object_id)
    stage_obj = VersionState(
        state=inv.versions[head].state,
        digest_algorithm=alg,
        content_source=content_source,
    )
    plan = sr.plan(object_id, stage_obj, message=message, user=user, created=created)
    return plan, content_source


def load_plan(staging_dir: Path) -> CommitPlan | None:
    """The staged `plan.json`, or None if it is missing or unreadable."""
    try:
        return CommitPlan.from_json(plan_path(staging_dir).read_bytes())
    except FileNotFoundError, PlanMismatchError:
        return None


def missing_metadata_fields(metadata: StagedMetadata) -> list[str]:
    """Return required staged metadata fields that are not present."""
    user = metadata.user
    missing: list[str] = []
    if metadata.message is None:
        missing.append("message")
    if user is None or user.name is None:
        missing.append("user.name")
    if user is None or user.address is None:
        missing.append("user.address")
    return missing


def require_staged_metadata(
    metadata: StagedMetadata, *, allow_missing_metadata: bool
) -> None:
    """Enforce required staged metadata unless the caller explicitly bypasses."""
    if allow_missing_metadata:
        return
    missing = missing_metadata_fields(metadata)
    if missing:
        fields = ", ".join(missing)
        raise MissingStagedMetadataError(
            f"missing staged metadata: {fields} "
            "(use --allow-missing-metadata to bypass)"
        )


def require_timestamped_plan_metadata(
    version: VersionInfo, *, allow_missing_metadata: bool
) -> None:
    """Enforce required metadata on the immutable timestamped inventory plan."""
    if allow_missing_metadata:
        return
    missing = missing_metadata_fields(_staged_metadata_from_version(version))
    if missing:
        fields = ", ".join(missing)
        raise MissingStagedMetadataError(
            f"timestamped inventory plan missing metadata: {fields} "
            "(rerun stage timestamp inventory after fixing staged metadata, "
            "or use --allow-missing-metadata to bypass)"
        )


def version_user_from_staged(metadata: StagedMetadata) -> UserInfo | None:
    user = metadata.user
    if user is None or user.name is None:
        return None
    return UserInfo(name=user.name, address=user.address)


def _staged_metadata_from_version(version: VersionInfo) -> StagedMetadata:
    user = version.user
    return StagedMetadata(
        message=version.message,
        user=None if user is None else StagedUser(name=user.name, address=user.address),
    )


def commit_bare(
    *,
    store: StagingStore,
    sr: StorageRoot,
    fs: WriteFS,
    root_path: str,
    object_id: str,
    staging_dir: Path,
    allow_missing_metadata: bool,
) -> None:
    """`stage commit` (non-OTS path): build the plan, commit it, purge staging."""
    with (
        object_commit_lock(root_path, object_id, timeout=lock_timeout()),
        with_lock(staging_dir, timeout=lock_timeout()),
    ):
        metadata = store.load_metadata(root_path, object_id)
        require_staged_metadata(metadata, allow_missing_metadata=allow_missing_metadata)
        plan, content_source = build_inventory_plan(
            store,
            sr,
            root_path,
            object_id,
            message=metadata.message,
            user=version_user_from_staged(metadata),
        )
        _commit_plan(sr, fs, plan, content_source)
        store.purge(root_path, object_id)


def _commit_plan(
    sr: StorageRoot, fs: WriteFS, plan: CommitPlan, content_source: ContentSource
) -> None:
    """Commit `plan`. The lib reverts a failed commit; staging is kept for retry."""
    try:
        sr.commit(fs, plan.object_id, plan, content=content_source)
    except ClaimLostError as exc:
        raise CommitClaimLostError(str(exc)) from exc
    except ObjectLockedError as exc:
        raise LockTimeoutError(f"object commit lock busy: {exc}") from exc
    except KeyboardInterrupt:
        raise CommitInterruptedError("commit interrupted, reverting...") from None
    except Exception as exc:
        raise CommitFailedError(str(exc)) from exc


def commit_ots(
    *,
    store: StagingStore,
    sr: StorageRoot,
    fs: WriteFS,
    root_path: str,
    object_id: str,
    staging_dir: Path,
    allow_missing_metadata: bool,
) -> None:
    """Attach the cached inventory proof to the timestamped plan and commit it.

    Caller catches `AttestationMismatchError` (-> exit 5) and `PhaseError`
    (-> exit 3). The staging lock is released before the commit, which
    writes the OCFL object, not staging, so a concurrent `--upgrade --watch`
    can keep running.
    """
    with object_commit_lock(root_path, object_id, timeout=lock_timeout()):
        with with_lock(staging_dir, timeout=lock_timeout()):
            state = load_ots(staging_dir)
            target_phase = check_transition(state.phase, "commit (ots path)")
            require_verified_content_before_inventory(state)

            if (
                state.inventory_proof is None
                or not state.inventory_proof.proof.verified
            ):
                raise AttestationMismatchError(
                    "inventory_proof missing or unverified; "
                    "re-run `stage timestamp inventory --upgrade`"
                )

            inv_digest = state.inventory_proof.digest
            plan = load_plan(staging_dir)
            if plan is None:
                raise PhaseError(
                    "inventory timestamp plan missing or stale; "
                    "run `stage timestamp inventory`"
                )
            if plan.inventory.digest != inv_digest:
                raise AttestationMismatchError(
                    f"inventory timestamp plan digest {plan.inventory.digest} "
                    f"does not match verified inventory digest {inv_digest}; "
                    "re-run `stage timestamp inventory` and "
                    "`stage timestamp inventory --upgrade`"
                )
            version = plan.inventory.inventory.versions[plan.version]
            require_timestamped_plan_metadata(
                version,
                allow_missing_metadata=allow_missing_metadata,
            )

            try:
                inv_ots = ots_cache_path(staging_dir, inv_digest).read_bytes()
            except FileNotFoundError:
                raise AttestationMismatchError(
                    f"ots-cache/{inv_digest}.ots missing; "
                    f"re-run `stage timestamp inventory --upgrade`"
                ) from None

            try:
                parsed = parse_ots(inv_ots)
            except OtsParseError as exc:
                raise AttestationMismatchError(
                    f"failed to parse cached inventory .ots ({exc}); "
                    f"re-run `stage timestamp inventory --upgrade`"
                ) from None

            if parsed.commitment_digest.hex() != inv_digest:
                raise AttestationMismatchError(
                    f"cached inventory .ots commits to "
                    f"{parsed.commitment_digest.hex()}, plan expects "
                    f"{inv_digest}; re-run `stage timestamp inventory --upgrade`"
                )

            plan = dataclasses.replace(plan, inventory_proof=inv_ots)
            content_source = _validate_and_build_content_source(
                store, root_path, object_id
            )
            prev_phase = state.phase

        _commit_plan(sr, fs, plan, content_source)

        # Purge staging on success (also removes ots-cache/, ots.json, plan.json).
        store.purge(root_path, object_id)
        log_phase(prev_phase, target_phase)


def staged_content_for_recovery(
    staging_root: Path, root_path: str, object_id: str
) -> ContentSource | None:
    """The object's staged content for `recover --resume`, if still staged."""
    if not staging_root.is_dir():
        return None
    store = StagingStore(staging_root)
    if not store.exists(root_path, object_id):
        return None
    return staged_content_source(store, root_path, object_id)
