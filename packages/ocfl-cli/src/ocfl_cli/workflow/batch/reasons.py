from typing import TYPE_CHECKING, Literal

from ocfl_cli import errors as errors
from ocfl_cli.workflow import hints

if TYPE_CHECKING:
    from ocfl_cli.model import batch as batch_model


BATCH_MISSING_NEXT = "work per object: ocfl stage status OBJECT"


type FixedBatchReason = Literal[
    "attestation_mismatch",
    "batch_file_missing",
    "committed",
    "content_not_upgraded",
    "lock_contended",
    "missing_metadata",
    "missing_record_ids",
    "no_new_digests",
    "noop",
    "not_ots",
    "not_staged",
    "regressed",
    "reorg_deeper_than_min_conf",
    "results_expired",
    "sp_partial_results",
    "submit_pending",
    "transient_rpc_error",
    "transient_sp_error",
    "verify_job_failed",
]


type PrefixedBatchReason = Literal["category_mismatch", "in_batch"]


FIXED_BATCH_REASONS: tuple[FixedBatchReason, ...] = (
    "attestation_mismatch",
    "batch_file_missing",
    "committed",
    "content_not_upgraded",
    "lock_contended",
    "missing_metadata",
    "missing_record_ids",
    "no_new_digests",
    "noop",
    "not_ots",
    "not_staged",
    "regressed",
    "reorg_deeper_than_min_conf",
    "results_expired",
    "sp_partial_results",
    "submit_pending",
    "transient_rpc_error",
    "transient_sp_error",
    "verify_job_failed",
)


def fixed_batch_reason(reason: FixedBatchReason) -> FixedBatchReason:
    return reason


def in_batch_reason(batch_id: str) -> str:
    return f"in_batch={batch_id}"


def category_mismatch_reason(category_id: int) -> str:
    return f"category_mismatch={category_id}"


def split_prefixed_batch_reason(reason: str) -> tuple[PrefixedBatchReason, str] | None:
    match reason.split("=", maxsplit=1):
        case ["in_batch", batch_id]:
            return "in_batch", batch_id
        case ["category_mismatch", category_id]:
            return "category_mismatch", category_id
        case _:
            return None


def reason_next(
    reason: str | None, *, batch: batch_model.BatchFile, object_id: str
) -> str | None:
    if reason is None:
        return None
    match split_prefixed_batch_reason(reason):
        case ("in_batch", batch_id):
            return hints.batch("status", batch_id)
        case ("category_mismatch", _category_id):
            return (
                "Start a separate batch for that category, or discard the "
                "staging and stage the object again"
            )
        case None:
            pass
    match reason:
        case "regressed":
            return hints.stage("status", object_id)
        case "missing_record_ids" | "transient_sp_error" | "submit_pending":
            return hints.batch("retry", batch.batch_id)
        case "lock_contended":
            return (
                f"{hints.batch('submit', batch.batch_id)} "
                "(after the other process exits)"
            )
        case "committed" | "no_new_digests" | "noop":
            return "None"
        case "not_staged":
            return hints.stage("new", object_id, "--ots")
        case "not_ots":
            return (
                f"{hints.stage('discard', object_id)}, then "
                f"{hints.stage('new', object_id, '--ots')}"
            )
        case "content_not_upgraded":
            return hints.stage("upgrade", object_id, "--watch")
        case "missing_metadata":
            return (
                f"{hints.stage('metadata', object_id)} ... "
                "(or create the batch with --allow-missing-metadata)"
            )
        case "sp_partial_results":
            return f"{hints.batch('upgrade', batch.batch_id)} (later)"
        case "results_expired" | "verify_job_failed" | "transient_rpc_error":
            return hints.batch("upgrade", batch.batch_id)
        case "reorg_deeper_than_min_conf":
            return "Retry after node catches up or investigate chain state"
        case "attestation_mismatch":
            return "Operator inspection required"
        case "batch_file_missing":
            return BATCH_MISSING_NEXT
        case _:
            return "Inspect batch status"
