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
    plan_path,
    save_ots,
    with_lock,
)
from ocfl_cli.workflow import common as wf_common
from ocfl_cli.workflow import content as wf_content
from ocfl_cli.workflow import inventory as wf_inventory
from ocfl_cli.workflow.batch.reasons import (
    category_mismatch_reason,
    fixed_batch_reason,
    in_batch_reason,
)

if TYPE_CHECKING:
    from ocfl_cli.model.ots_state import OtsState
    from ocfl_cli.store.staging import StagingStore


def read_objects_file(path: Path) -> list[str]:
    object_ids: list[str] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            object_ids.append(stripped)
    return object_ids


def replace_batch_object(
    batch: batch_model.BatchFile, row: batch_model.BatchObject
) -> batch_model.BatchFile:
    rows = [
        existing for existing in batch.objects if existing.object_id != row.object_id
    ]
    rows.append(row)
    return msgspec.structs.replace(
        batch, objects=tuple(rows), updated_at=batch_state.now_utc_iso_z()
    )


def object_has_other_batch(
    state: OtsState, batch_id: str, target: batch_model.BatchTarget
) -> str | None:
    if target == "inventory":
        proof = state.inventory_proof
        if (
            proof is not None
            and proof.batch_id is not None
            and proof.batch_id != batch_id
            and proof.record_id is None
        ):
            return proof.batch_id
        return None
    for submission in state.submissions:
        if (
            submission.status in {"submitting", "submitted"}
            and submission.batch_id is not None
            and submission.batch_id != batch_id
        ):
            return submission.batch_id
    return None


def batch_command_tuple(target: str, batch_id: str) -> tuple[str, ...]:
    return ("stage", "timestamp", "batch", target, "--batch", batch_id)


def prepare_batch_object(
    *,
    target: batch_model.BatchTarget,
    cfg,
    store: StagingStore,
    sr,
    root_path: str,
    staging_root: Path,
    batch: batch_model.BatchFile,
    object_id: str,
    allow_missing_metadata: bool,
) -> batch_model.BatchObject:
    staging_dir = store.staging_path(root_path, object_id)
    if not store.exists(root_path, object_id) or not has_ots(staging_dir):
        return batch_model.BatchObject(
            object_id=object_id,
            status="skipped",
            skipped_reason=fixed_batch_reason("committed"),
        )
    try:
        with with_lock(staging_dir, timeout=wf_common.LOCK_TIMEOUT_SECONDS):
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
            if target == "content":
                prepared = wf_content.prepare_content_submission_locked(
                    cfg, store, sr, root_path, object_id, staging_dir
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
                submissions = list(state.submissions)
                submissions[-1] = msgspec.structs.replace(
                    submissions[-1],
                    batch_id=batch.batch_id,
                    record_ids=tuple(None for _ in prepared.digests),
                )
                save_ots(
                    staging_dir,
                    msgspec.structs.replace(state, submissions=tuple(submissions)),
                )
                return batch_model.BatchObject(
                    object_id=object_id, status="prepared", digests=prepared.digests
                )

            prepared = wf_inventory.prepare_inventory_submission_locked(
                cfg,
                store,
                sr,
                root_path,
                object_id,
                staging_dir,
                allow_missing_metadata=allow_missing_metadata,
            )
            if prepared.digest is None:
                return batch_model.BatchObject(
                    object_id=object_id,
                    status="skipped",
                    skipped_reason=fixed_batch_reason("noop"),
                )
            state = load_ots(staging_dir)
            if state.inventory_proof is None:
                return batch_model.BatchObject(
                    object_id=object_id,
                    status="failed",
                    last_error=fixed_batch_reason("regressed"),
                )
            save_ots(
                staging_dir,
                msgspec.structs.replace(
                    state,
                    inventory_proof=msgspec.structs.replace(
                        state.inventory_proof,
                        batch_id=batch.batch_id,
                        record_id=None,
                    ),
                ),
            )
            if prepared.plan is not None:
                plan_path(staging_dir).write_bytes(prepared.plan.to_json())
            return batch_model.BatchObject(
                object_id=object_id, status="prepared", digests=(prepared.digest,)
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
        reason = (
            fixed_batch_reason("content_not_upgraded")
            if target == "inventory"
            else fixed_batch_reason("no_new_digests")
        )
        return batch_model.BatchObject(
            object_id=object_id, status="skipped", skipped_reason=reason
        )


def collect_recoverable_object_intents(
    store: StagingStore, batch: batch_model.BatchFile
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
        with with_lock(staging_dir, timeout=wf_common.LOCK_TIMEOUT_SECONDS):
            state = load_ots(staging_dir)
            row = recover_content_intent(batch, object_id, state)
            if batch.target == "inventory":
                row = recover_inventory_intent(batch, object_id, state)
        if row is not None:
            recovered.append(row)
            known.add(object_id)
    return tuple(recovered)


def merge_recovered_object_intents(
    batch: batch_model.BatchFile, recovered: tuple[batch_model.BatchObject, ...]
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
        batch, objects=tuple(merged), updated_at=batch_state.now_utc_iso_z()
    )


def recover_content_intent(
    batch: batch_model.BatchFile, object_id: str, state: OtsState
) -> batch_model.BatchObject | None:
    for submission in state.submissions:
        if submission.batch_id != batch.batch_id:
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


def recover_inventory_intent(
    batch: batch_model.BatchFile, object_id: str, state: OtsState
) -> batch_model.BatchObject | None:
    proof = state.inventory_proof
    if proof is None or proof.batch_id != batch.batch_id:
        return None
    return batch_model.BatchObject(
        object_id=object_id,
        status="submitted" if proof.record_id is not None else "prepared",
        digests=(proof.digest,),
    )
