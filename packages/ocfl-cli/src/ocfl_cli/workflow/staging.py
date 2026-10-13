"""Staging-area use cases: metadata, and keeping OTS state in step with edits."""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from msgspec.structs import replace
from platformdirs import user_state_path

from ocfl_cli.config.archive import validate_digest_name
from ocfl_cli.errors import CLIError, StagingError, UsageError
from ocfl_cli.model.metadata import StagedMetadata, StagedUser
from ocfl_cli.model.ots_state import OtsState
from ocfl_cli.model.phase import Phase, check_transition
from ocfl_cli.store.ingest import ingest_file
from ocfl_cli.store.ots_state import (
    has_ots,
    load_ots,
    save_ots,
    with_lock,
)
from ocfl_cli.store.staging import StagingStore
from ocfl_cli.workflow.common import (
    StateMap,
    ordinary_digests,
    ordinary_paths,
    update_head_state,
)
from ocfl_lib import DigestAlgorithm, InventoryBuilder, ots

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ocfl_cli.ports import Runtime
    from ocfl_lib import StorageRoot

logger = logging.getLogger(__name__)


class MetadataInputError(ValueError):
    """Invalid staged metadata CLI input."""


def _validate_metadata_input(value: str | None, *, field: str) -> str | None:
    if value is not None and not value.strip():
        raise MetadataInputError(f"staged metadata {field} cannot be empty")
    return value


def _check_metadata_conflict(
    *,
    field: str,
    value: str | None,
    clear: bool,
) -> None:
    if value is not None and clear:
        raise MetadataInputError(f"--{field} is incompatible with --clear-{field}")


def _compact_user(name: str | None, address: str | None) -> StagedUser | None:
    if name is None and address is None:
        return None
    return StagedUser(name=name, address=address)


def staged_metadata_from_options(
    *,
    message: str | None,
    name: str | None,
    address: str | None,
) -> StagedMetadata:
    """Build staged metadata from initial CLI options."""
    message = _validate_metadata_input(message, field="message")
    name = _validate_metadata_input(name, field="user.name")
    address = _validate_metadata_input(address, field="user.address")
    return StagedMetadata(
        message=message,
        user=_compact_user(name, address),
    )


def validate_metadata_update_options(
    *,
    message: str | None,
    name: str | None,
    address: str | None,
    clear_message: bool,
    clear_name: bool,
    clear_address: bool,
) -> None:
    """Validate staged metadata update options without reading staging state."""
    _check_metadata_conflict(field="message", value=message, clear=clear_message)
    _check_metadata_conflict(field="name", value=name, clear=clear_name)
    _check_metadata_conflict(field="address", value=address, clear=clear_address)

    _validate_metadata_input(message, field="message")
    _validate_metadata_input(name, field="user.name")
    _validate_metadata_input(address, field="user.address")


def update_metadata(
    store: StagingStore,
    root_path: str,
    object_id: str,
    *,
    message: str | None,
    name: str | None,
    address: str | None,
    clear_message: bool,
    clear_name: bool,
    clear_address: bool,
    before_save: Callable[[StagedMetadata, StagedMetadata], None] | None = None,
) -> StagedMetadata:
    """Apply set/update/clear semantics to staged metadata and persist it.

    ``before_save`` lets callers invalidate dependent state after the new
    metadata is known but before metadata.json is rewritten.
    """
    validate_metadata_update_options(
        message=message,
        name=name,
        address=address,
        clear_message=clear_message,
        clear_name=clear_name,
        clear_address=clear_address,
    )

    current = store.load_metadata(root_path, object_id)
    current_name = current.user.name if current.user is not None else None
    current_address = current.user.address if current.user is not None else None

    next_message = current.message
    if clear_message:
        next_message = None
    if message is not None:
        next_message = message

    next_name = current_name
    if clear_name:
        next_name = None
    if name is not None:
        next_name = name

    next_address = current_address
    if clear_address or (clear_name and address is None):
        next_address = None
    if address is not None:
        next_address = address

    updated = replace(
        current,
        message=next_message,
        user=_compact_user(next_name, next_address),
    )
    if before_save is not None:
        before_save(current, updated)
    store.save_metadata(root_path, object_id, updated)
    return updated


def pair_ots_companion_remove(state: StateMap, removed: dict[str, str]) -> None:
    """Remove the companion `P.ots` of each removed ordinary path `P`.

    `removed` maps each removed path to whether it was ordinary or a content
    proof in the state before the removal (`ordinary_paths`); removing a
    proof leaves its file to be proved again.
    """
    for p, kind in removed.items():
        if kind != "ordinary":
            continue
        companion = f"{p}{ots.PROOF_SUFFIX}"
        for d in list(state):
            if companion in state[d]:
                state[d].remove(companion)
                if not state[d]:
                    del state[d]
                break


def pair_ots_companion_rename(state: StateMap, src: str, dst: str) -> None:
    """Rename `<src>.ots` to `<dst>.ots` within *state* if present."""
    src_ots = f"{src}{ots.PROOF_SUFFIX}"
    dst_ots = f"{dst}{ots.PROOF_SUFFIX}"
    for d in state:
        if src_ots in state[d]:
            state[d].remove(src_ots)
            state[d].append(dst_ots)
            return


def path_kinds(state: StateMap, paths: list[str]) -> dict[str, str]:
    """`{path: "ordinary" | "proof"}` for `paths` in `state`."""
    ordinary = ordinary_paths(state)
    return {p: "ordinary" if p in ordinary else "proof" for p in paths}


def apply_rm_mv_regression(
    staging_dir: Path,
    new_inv_state: StateMap,
    command: str,
    cause: str,
) -> None:
    """Update `ots.json` for stage add/rm/mv regressions. Caller holds the lock.

    Proofs stay (ADR-0004): a path whose digest has one reuses it. The staged
    inventory is dropped; `plan.json` stays so a rebuilt inventory with the
    same bytes keeps its proof.
    """
    state = load_ots(staging_dir)
    new_phase = check_transition(state.phase, command)
    new_pending = tuple(
        d for d in state.pending_digests if d in ordinary_digests(new_inv_state)
    )
    inventory = state.inventory
    if state.phase in (Phase.INVENTORY_PENDING, Phase.INVENTORY_UPGRADED):
        inventory = None
    save_ots(
        staging_dir,
        replace(
            state,
            phase=new_phase,
            pending_digests=new_pending,
            inventory=inventory,
        ),
    )
    if new_phase is not state.phase:
        logger.info(
            "phase regression %s→%s (cause: %s)",
            state.phase.value,
            new_phase.value,
            cause,
        )


def apply_metadata_ots_regression(staging_dir: Path) -> None:
    """Move back to content-upgraded after a staged metadata change.

    Metadata affects the inventory bytes but not staged content bytes. Once an
    inventory timestamp exists, regress only the inventory side of the
    workflow. Every proof stays (ADR-0004): reverting the edit rebuilds the
    same inventory bytes, which reuse their proof.
    """
    state = load_ots(staging_dir)
    if state.phase not in (Phase.INVENTORY_PENDING, Phase.INVENTORY_UPGRADED):
        return
    save_ots(
        staging_dir,
        replace(state, phase=Phase.CONTENT_UPGRADED, inventory=None),
    )
    logger.info(
        "phase regression %s→%s (cause: staged metadata edit)",
        state.phase.value,
        Phase.CONTENT_UPGRADED.value,
    )


# ---------------------------------------------------------------------------
# Staging Area use cases behind `stage new|metadata|add|mv|rm|ls|reset`.
# ---------------------------------------------------------------------------


def default_staging_root() -> Path:
    """The per-user staging directory when `--staging-root` is not given."""
    return user_state_path("ocfl-cli") / "staging"


def open_store(staging_root: Path) -> StagingStore:
    """The Staging Area store under `staging_root`, created if missing."""
    staging_root.mkdir(parents=True, exist_ok=True)
    return StagingStore(staging_root)


def require_store(value: object) -> StagingStore:
    """`value` as the store `stage` opened; an internal error otherwise."""
    if not isinstance(value, StagingStore):
        raise RuntimeError("internal error: missing ctx.obj['staging']")
    return value


@dataclass(frozen=True, slots=True)
class StagingResolved:
    store: StagingStore
    root_path: str
    staging_dir: Path


def resolve_staging(
    store: StagingStore, root_path: str, object_id: str, *, hint: str | None = None
) -> StagingResolved:
    """The object's existing Staging Area; `StagingError` when there is none."""
    staging_dir = store.staging_path(root_path, object_id)
    if not store.exists(root_path, object_id):
        message = f"no staging exists for {object_id!r}"
        if hint is not None:
            message = f"{message} ({hint})"
        raise StagingError(message)
    return StagingResolved(store=store, root_path=root_path, staging_dir=staging_dir)


def is_ots_staging(staging_dir: Path) -> bool:
    """Whether the Staging Area runs the OTS workflow (has `ots.json`)."""
    return has_ots(staging_dir)


def require_ots_staging(staging_dir: Path, object_id: str) -> None:
    if not has_ots(staging_dir):
        raise StagingError(
            f"{object_id!r} is not an OTS staging (run 'stage new --ots' first)"
        )


def ots_state(staging_dir: Path) -> OtsState:
    """The Staging Area's OTS state, read without the lock."""
    return load_ots(staging_dir)


def validate_logical_path(logical_path: str) -> None:
    if (
        not logical_path
        or logical_path.startswith("/")
        or logical_path.endswith("/")
        or any(part in ("", ".", "..") for part in logical_path.split("/"))
    ):
        raise CLIError(f"invalid logical path: {logical_path!r}")


def new_staging(
    store: StagingStore,
    sr: StorageRoot,
    root_path: str,
    object_id: str,
    *,
    metadata: StagedMetadata,
    digest_name: str | None,
    ots_category_id: int | None,
) -> None:
    """Start a Staging Area from the object's head (or an empty new object).

    `digest_name` applies to a new object only (default sha512). With
    `ots_category_id` the area runs the OTS workflow, which needs sha256.
    """
    if store.exists(root_path, object_id):
        raise CLIError(f"staging already exists for {object_id!r}")

    obj = sr.get_object(object_id)
    if obj.exists:
        head_inv = obj.inventory.inventory
        if (
            ots_category_id is not None
            and head_inv.digest_algorithm != DigestAlgorithm.SHA256.ocfl_name
        ):
            raise UsageError(
                "--ots requires existing objects to use SHA-256 "
                f"(got {head_inv.digest_algorithm})"
            )
        builder = InventoryBuilder(head_inv).add_version(
            {}, created="1970-01-01T00:00:00Z"
        )
        inv = builder.build()
    else:
        alg = DigestAlgorithm.SHA512
        if digest_name:
            try:
                validate_digest_name(digest_name)
            except ValueError:
                raise UsageError("content digest must be sha512 or sha256") from None
            alg = DigestAlgorithm.from_ocfl_name(digest_name)
        inv = (
            InventoryBuilder()
            .id(object_id)
            .spec(sr.spec)
            .digest_algorithm(alg)
            .add_version({}, created="1970-01-01T00:00:00Z")
            .build()
        )

    store.create(root_path, object_id, inv)
    if metadata != StagedMetadata():
        store.save_metadata(root_path, object_id, metadata)

    if ots_category_id is not None:
        save_ots(
            store.staging_path(root_path, object_id),
            OtsState(phase=Phase.DRAFTING, category_id=ots_category_id),
        )


def edit_metadata(
    resolved: StagingResolved,
    object_id: str,
    *,
    message: str | None,
    name: str | None,
    address: str | None,
    clear_message: bool,
    clear_name: bool,
    clear_address: bool,
    runtime: Runtime,
) -> None:
    """Set or clear staged version metadata; regress OTS progress it invalidates."""
    validate_metadata_update_options(
        message=message,
        name=name,
        address=address,
        clear_message=clear_message,
        clear_name=clear_name,
        clear_address=clear_address,
    )
    with with_lock(resolved.staging_dir, timeout=runtime.lock_timeout):

        def invalidate_ots_if_needed(
            current: StagedMetadata, updated: StagedMetadata
        ) -> None:
            if has_ots(resolved.staging_dir) and updated != current:
                apply_metadata_ots_regression(resolved.staging_dir)

        update_metadata(
            resolved.store,
            resolved.root_path,
            object_id,
            message=message,
            name=name,
            address=address,
            clear_message=clear_message,
            clear_name=clear_name,
            clear_address=clear_address,
            before_save=invalidate_ots_if_needed,
        )


def add_sources(
    resolved: StagingResolved,
    object_id: str,
    sources: list[Path],
    *,
    as_path: str | None,
    command: str,
    runtime: Runtime,
) -> None:
    """Stage files and directories (by reference) at their logical paths."""
    store = resolved.store
    root_path = resolved.root_path
    staging_dir = resolved.staging_dir

    inv = store.load_inventory(root_path, object_id)
    alg = DigestAlgorithm.from_ocfl_name(inv.digest_algorithm)
    head = inv.head
    state = dict(inv.versions[head].state)
    content_map = store.load_content_map(root_path, object_id)

    path_to_digest: dict[str, str] = {}
    for d, paths in state.items():
        for p in paths:
            path_to_digest[p] = d

    prior_digests = set(inv.manifest.keys())
    for p in sources:
        if not p.exists():
            raise CLIError(f"source not found: {p}")
        if not ((p.is_file() and not p.is_symlink()) or p.is_dir()):
            raise CLIError(f"unsupported source: {p}")

    def _add_file(file_path: Path, logical_path: str) -> None:
        validate_logical_path(logical_path)
        result = ingest_file(
            file_path,
            logical_path,
            staging_dir=staging_dir,
            content_map=content_map,
            digest_alg=alg,
        )
        d = result.digest

        if d in prior_digests:
            content_map.pop(d, None)

        if logical_path in path_to_digest:
            old_d = path_to_digest[logical_path]
            old_paths = state.get(old_d, [])
            if logical_path in old_paths:
                old_paths.remove(logical_path)
                if not old_paths:
                    state.pop(old_d, None)

        if d in state:
            if logical_path not in state[d]:
                state[d].append(logical_path)
        else:
            state[d] = [logical_path]
        path_to_digest[logical_path] = d

    for p in sources:
        if p.is_file() and not p.is_symlink():
            logical = as_path or p.name
            _add_file(p, logical)
        elif p.is_dir():
            for child in sorted(p.rglob("*")):
                if child.is_symlink():
                    continue
                if not child.is_file():
                    continue
                rel = str(child.relative_to(p))
                logical = f"{as_path}/{rel}" if as_path else rel
                _add_file(child, logical)

    def _finalize() -> None:
        store.save_inventory(root_path, object_id, update_head_state(inv, state))
        store.save_content_map(root_path, object_id, content_map)

    if has_ots(staging_dir):
        with with_lock(staging_dir, timeout=runtime.lock_timeout):
            _finalize()
            apply_rm_mv_regression(staging_dir, state, "add", command)
    else:
        _finalize()


def move_path(
    resolved: StagingResolved,
    object_id: str,
    src_path: str,
    dst_path: str,
    *,
    runtime: Runtime,
) -> None:
    """Rename a logical path (and its `.ots` companion in an OTS area)."""
    store = resolved.store
    root_path = resolved.root_path
    staging_dir = resolved.staging_dir

    inv = store.load_inventory(root_path, object_id)
    state = {d: list(ps) for d, ps in inv.versions[inv.head].state.items()}

    found_digest = None
    for d, paths in state.items():
        if src_path in paths:
            found_digest = d
            break

    if found_digest is None:
        raise CLIError(f"path not found: {src_path!r}")

    validate_logical_path(dst_path)

    def _path_exists(path: str) -> bool:
        return any(path in paths for paths in state.values())

    if dst_path != src_path and _path_exists(dst_path):
        raise CLIError(f"path already exists: {dst_path!r}")

    moves_companion = (
        has_ots(staging_dir) and path_kinds(state, [src_path])[src_path] == "ordinary"
    )
    if moves_companion:
        src_ots = f"{src_path}{ots.PROOF_SUFFIX}"
        dst_ots = f"{dst_path}{ots.PROOF_SUFFIX}"
        if src_ots != dst_ots and _path_exists(src_ots) and _path_exists(dst_ots):
            raise CLIError(f"path already exists: {dst_ots!r}")

    state[found_digest].remove(src_path)
    state[found_digest].append(dst_path)

    if has_ots(staging_dir):
        with with_lock(staging_dir, timeout=runtime.lock_timeout):
            if moves_companion:
                pair_ots_companion_rename(state, src_path, dst_path)
            store.save_inventory(root_path, object_id, update_head_state(inv, state))
            apply_rm_mv_regression(
                staging_dir, state, "mv", f"stage mv {src_path} {dst_path}"
            )
    else:
        store.save_inventory(root_path, object_id, update_head_state(inv, state))


def remove_paths(
    resolved: StagingResolved,
    object_id: str,
    paths: list[str],
    *,
    recursive: bool,
    runtime: Runtime,
) -> None:
    """Remove logical paths (prefixes with `recursive`) from the staged version."""
    store = resolved.store
    root_path = resolved.root_path
    staging_dir = resolved.staging_dir

    inv = store.load_inventory(root_path, object_id)
    state = {d: list(ps) for d, ps in inv.versions[inv.head].state.items()}
    before = {d: list(ps) for d, ps in state.items()}
    content_map = store.load_content_map(root_path, object_id)

    removed_paths: list[str] = []
    for target in paths:
        if recursive:
            prefix = target.rstrip("/") + "/"
            for d in list(state):
                kept: list[str] = []
                for p in state[d]:
                    if p == target or p.startswith(prefix):
                        removed_paths.append(p)
                    else:
                        kept.append(p)
                if kept:
                    state[d] = kept
                else:
                    del state[d]
        else:
            for d in list(state):
                if target in state[d]:
                    state[d].remove(target)
                    if not state[d]:
                        del state[d]
                    removed_paths.append(target)
                    break
            else:
                raise CLIError(f"path not found: {target!r}")

    if not removed_paths:
        raise CLIError("no matching paths found")

    def _finalize() -> None:
        referenced_digests = set(state.keys())
        for d in list(content_map):
            if d not in referenced_digests:
                del content_map[d]
        store.save_inventory(root_path, object_id, update_head_state(inv, state))
        store.save_content_map(root_path, object_id, content_map)

    if has_ots(staging_dir):
        with with_lock(staging_dir, timeout=runtime.lock_timeout):
            pair_ots_companion_remove(state, path_kinds(before, removed_paths))
            _finalize()
            apply_rm_mv_regression(
                staging_dir, state, "rm", f"stage rm {' '.join(paths)}"
            )
    else:
        _finalize()


def list_entries(resolved: StagingResolved, object_id: str) -> list[tuple[str, str]]:
    """`(logical path, digest)` pairs of the staged version, sorted by path."""
    inv = resolved.store.load_inventory(resolved.root_path, object_id)
    state = inv.versions[inv.head].state
    return sorted((p, d) for d, paths in state.items() for p in paths)


def _active_reset_batch_ids(state: OtsState) -> tuple[str, ...]:
    batch_ids = (
        *(
            submission.batch_id
            for submission in state.submissions
            if submission.status in {"submitting", "submitted"}
            and submission.batch_id is not None
        ),
        *(
            ()
            if state.inventory is None or state.inventory.batch_id is None
            else (state.inventory.batch_id,)
        ),
    )
    return tuple(dict.fromkeys(batch_ids))


def reset_staging(
    store: StagingStore, root_path: str, object_id: str, *, force: bool
) -> bool:
    """Delete the Staging Area; False when there is none.

    Refuses (`CLIError`) to discard persisted OTS progress unless `force`.
    """
    if not store.exists(root_path, object_id):
        return False

    staging_dir = store.staging_path(root_path, object_id)
    if has_ots(staging_dir):
        state = load_ots(staging_dir)
        counts = (
            ("submissions", len(state.submissions)),
            ("verify_jobs", len(state.verify_jobs)),
            ("proofs", len(state.proofs)),
        )
        if any(n for _, n in counts):
            phase = state.phase.value
            summary = ", ".join(f"{k}={v}" for k, v in counts)
            active_batch_ids = _active_reset_batch_ids(state)
            batch_hint = ""
            if active_batch_ids:
                batch_id = active_batch_ids[0]
                batch_hint = (
                    f"; active batch_id={batch_id!r}; inspect with "
                    f"'stage timestamp batch status {batch_id}'"
                )
            if not force:
                raise CLIError(
                    f"reset would discard persisted OTS progress at phase "
                    f"{phase!r} ({summary}){batch_hint}; "
                    f"re-run with --force to proceed"
                )
            logger.warning(
                "reset --force discarding OTS progress at phase %r: %s",
                phase,
                summary,
            )

    store.purge(root_path, object_id)
    return True
