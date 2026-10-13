"""Batch creation: pick the objects, prepare each row, recover lost intents."""

from pathlib import Path  # noqa: TC003 - Typer reads option annotations at runtime.
from typing import TYPE_CHECKING

import msgspec

from ocfl_cli import errors as errors
from ocfl_cli.errors import (
    LockTimeoutError,
    MissingStagedMetadataError,
    PhaseError,
)
from ocfl_cli.model import batch as batch_model
from ocfl_cli.store import batch as batch_state
from ocfl_cli.store.ots_state import (
    has_ots,
    load_ots,
    save_ots,
    with_lock,
)
from ocfl_cli.workflow import timestamp
from ocfl_cli.workflow.batch.reasons import (
    category_mismatch_reason,
    fixed_batch_reason,
    in_batch_reason,
)
from ocfl_cli.workflow.targets import Area, rules_for

if TYPE_CHECKING:
    from ocfl_cli.model.ots_state import OtsState
    from ocfl_cli.model.phase import TimestampTarget
    from ocfl_cli.ports import Runtime
    from ocfl_cli.store.staging import StagingStore


def read_objects_file(path: Path) -> list[str]:
    object_ids: list[str] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            object_ids.append(stripped)
    return object_ids


def replace_batch_object(
    batch: batch_model.BatchFile, row: batch_model.BatchObject, *, runtime: Runtime
) -> batch_model.BatchFile:
    rows = [
        existing for existing in batch.objects if existing.object_id != row.object_id
    ]
    rows.append(row)
    return msgspec.structs.replace(
        batch,
        objects=tuple(rows),
        updated_at=batch_state.utc_iso_z(runtime.clock.now()),
    )


def object_has_other_batch(
    state: OtsState, batch_id: str, target: TimestampTarget
) -> str | None:
    """The other batch still waiting on one of the object's submissions of `target`."""
    for submission in state.submissions:
        if (
            submission.target is target
            and submission.status in {"submitting", "submitted"}
            and submission.batch_id is not None
            and submission.batch_id != batch_id
            and not all(state.verified(d) for d in submission.digests)
        ):
            return submission.batch_id
    return None


def prepare_batch_object(
    *,
    target: TimestampTarget,
    cfg,
    store: StagingStore,
    sr,
    root_path: str,
    staging_root: Path,
    batch: batch_model.BatchFile,
    object_id: str,
    allow_missing_metadata: bool,
    runtime: Runtime,
) -> batch_model.BatchObject:
    staging_dir = store.staging_path(root_path, object_id)
    if not store.exists(root_path, object_id) or not has_ots(staging_dir):
        reason = "not_ots" if store.exists(root_path, object_id) else "not_staged"
        return batch_model.BatchObject(
            object_id=object_id,
            status="skipped",
            skipped_reason=fixed_batch_reason(reason),
        )
    try:
        with with_lock(staging_dir, runtime):
            state = load_ots(staging_dir)
            if state.category_id != batch.category_id:
                return batch_model.BatchObject(
                    object_id=object_id,
                    status="skipped",
                    skipped_reason=category_mismatch_reason(state.category_id),
                )
            if (
                other := object_has_other_batch(state, batch.batch_id, target)
            ) is not None:
                return batch_model.BatchObject(
                    object_id=object_id,
                    status="skipped",
                    skipped_reason=in_batch_reason(other),
                )
            prepared = timestamp.prepare_submission_locked(
                Area(
                    store=store,
                    sr=sr,
                    root_path=root_path,
                    object_id=object_id,
                    staging_dir=staging_dir,
                ),
                category_config=cfg.simpleproof.proof_category_id,
                runtime=runtime,
                expect=target,
                allow_missing_metadata=allow_missing_metadata,
            )
            if not prepared.digests:
                return batch_model.BatchObject(
                    object_id=object_id,
                    status="skipped",
                    skipped_reason=(
                        fixed_batch_reason("noop")
                        if prepared.noop_advanced
                        else fixed_batch_reason("no_new_digests")
                    ),
                )
            state = load_ots(staging_dir)
            submissions = tuple(
                msgspec.structs.replace(
                    s,
                    batch_id=batch.batch_id,
                    record_ids=tuple(None for _ in s.digests),
                )
                if s == prepared.submission
                else s
                for s in state.submissions
            )
            save_ots(
                staging_dir, msgspec.structs.replace(state, submissions=submissions)
            )
            return batch_model.BatchObject(
                object_id=object_id, status="prepared", digests=prepared.digests
            )
    except LockTimeoutError:
        return batch_model.BatchObject(
            object_id=object_id,
            status="skipped",
            skipped_reason=fixed_batch_reason("lock_contended"),
        )
    except MissingStagedMetadataError:
        return batch_model.BatchObject(
            object_id=object_id,
            status="skipped",
            skipped_reason=fixed_batch_reason("missing_metadata"),
        )
    except PhaseError:
        reason = fixed_batch_reason(rules_for(target).not_ready_reason)
        return batch_model.BatchObject(
            object_id=object_id, status="skipped", skipped_reason=reason
        )


def collect_recoverable_object_intents(
    store: StagingStore, batch: batch_model.BatchFile, *, runtime: Runtime
) -> tuple[batch_model.BatchObject, ...]:
    known = {row.object_id for row in batch.objects}
    recovered: list[batch_model.BatchObject] = []
    for staging_dir in sorted(store.root.iterdir()):
        if not staging_dir.is_dir() or not has_ots(staging_dir):
            continue
        inventory_path = staging_dir / "inventory.json"
        if not inventory_path.is_file():
            continue
        try:
            raw_inventory = msgspec.json.decode(inventory_path.read_bytes())
        except OSError, msgspec.DecodeError:
            continue
        if not isinstance(raw_inventory, dict):
            continue
        object_id = raw_inventory.get("id")
        if not isinstance(object_id, str) or object_id in known:
            continue
        if store.staging_path(batch.root_path, object_id) != staging_dir:
            continue
        with with_lock(staging_dir, runtime):
            row = recover_intent(batch, object_id, load_ots(staging_dir))
        if row is not None:
            recovered.append(row)
            known.add(object_id)
    return tuple(recovered)


def merge_recovered_object_intents(
    batch: batch_model.BatchFile,
    recovered: tuple[batch_model.BatchObject, ...],
    *,
    runtime: Runtime,
) -> batch_model.BatchFile:
    if not recovered:
        return batch
    known = {row.object_id for row in batch.objects}
    merged = list(batch.objects)
    for row in recovered:
        if row.object_id not in known:
            merged.append(row)
            known.add(row.object_id)
    if len(merged) == len(batch.objects):
        return batch
    return msgspec.structs.replace(
        batch,
        objects=tuple(merged),
        updated_at=batch_state.utc_iso_z(runtime.clock.now()),
    )


def recover_intent(
    batch: batch_model.BatchFile, object_id: str, state: OtsState
) -> batch_model.BatchObject | None:
    """The row of the object's submission tagged with this batch, if any."""
    for submission in state.submissions:
        if (
            submission.batch_id != batch.batch_id
            or submission.target is not batch.target
        ):
            continue
        submitted = submission.record_ids is not None and all(
            record_id is not None for record_id in submission.record_ids
        )
        return batch_model.BatchObject(
            object_id=object_id,
            status="submitted" if submitted else "prepared",
            digests=submission.digests,
        )
    return None
