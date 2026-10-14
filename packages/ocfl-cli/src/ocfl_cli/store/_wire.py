"""ots.json wire-shape conversion.

Schema 2 is the msgspec shape of `OtsState` with absent optional fields
omitted. Keep that boundary here so future schema migrations have one
serialization file to update.
"""

import copy
import logging
from typing import TYPE_CHECKING, Any

import msgspec

from ocfl_cli.errors import StagingError

if TYPE_CHECKING:
    from ocfl_cli.model.ots_state import OtsState


logger = logging.getLogger(__name__)


def encode_ots_state(state: OtsState) -> dict[str, Any]:
    raw = msgspec.to_builtins(state)
    _omit_absent_proof_fields(raw)
    _omit_absent_batch_fields(raw)
    _omit_absent_verify_job_fields(raw)
    return raw


def decode_ots_state(raw: dict[str, Any]) -> OtsState:
    from ocfl_cli.model.ots_state import VERIFY_JOB_STATUSES, OtsState

    raw = copy.deepcopy(raw)
    try:
        state = msgspec.convert(raw, OtsState)
    except msgspec.ValidationError as e:
        raise StagingError(f"ots.json: invalid OTS state: {e}") from None
    for job in state.verify_jobs:
        if job.status not in VERIFY_JOB_STATUSES:
            logger.warning(
                "unknown verify job status %r for job %s; treating as in-progress",
                job.status,
                job.job_id,
            )
    return state


def _omit_absent_proof_fields(raw: dict[str, Any]) -> None:
    proofs = raw.get("proofs")
    if isinstance(proofs, dict):
        for proof in proofs.values():
            if isinstance(proof, dict):
                _pop_none(proof, "block_hash")


def _omit_absent_batch_fields(raw: dict[str, Any]) -> None:
    submissions = raw.get("submissions")
    if isinstance(submissions, list | tuple):
        for submission in submissions:
            if isinstance(submission, dict):
                _pop_none(submission, "batch_id")
                _pop_none(submission, "record_ids")


def _omit_absent_verify_job_fields(raw: dict[str, Any]) -> None:
    verify_jobs = raw.get("verify_jobs")
    if not isinstance(verify_jobs, list | tuple):
        return
    for job in verify_jobs:
        if not isinstance(job, dict):
            continue
        for key in (
            "total_hashes",
            "verified_hashes",
            "created_at",
            "updated_at",
            "results_url",
            "last_snapshot_updated_at",
            "submitted_hashes",
            "total_documents_found",
        ):
            _pop_none(job, key)
        if job.get("attestation_snapshots") == {}:
            job.pop("attestation_snapshots", None)


def _pop_none(raw: dict[str, Any], key: str) -> None:
    if raw.get(key) is None:
        raw.pop(key, None)
