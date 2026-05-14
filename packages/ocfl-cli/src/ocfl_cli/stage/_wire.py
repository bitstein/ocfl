"""ots.json wire-shape conversion.

The in-memory `InventoryProof` keeps a nested `VerifiedProof`, while the v1
JSON shape flattens those proof fields into the top-level `inventory_proof`
object. Keep that compatibility boundary here so future schema migrations have
one serialization file to update.
"""

import copy
from typing import TYPE_CHECKING, Any

import msgspec

from ocfl_cli.errors import StagingError

if TYPE_CHECKING:
    from ocfl_cli.stage.state import OtsState


def encode_ots_state(state: OtsState) -> dict[str, Any]:
    """Encode an in-memory OTS state into the current ots.json wire shape."""
    raw = msgspec.to_builtins(state)
    _flatten_inventory_proof(raw)
    _omit_absent_batch_fields(raw)
    return raw


def decode_ots_state(raw: dict[str, Any]) -> OtsState:
    """Decode a current-schema ots.json dict. No I/O, no migrations."""
    from ocfl_cli.stage.state import OtsState

    raw = copy.deepcopy(raw)
    _nest_inventory_proof(raw)
    try:
        return msgspec.convert(raw, OtsState)
    except msgspec.ValidationError as e:
        raise StagingError(f"ots.json: invalid OTS state: {e}") from None


def _flatten_inventory_proof(raw: dict[str, Any]) -> None:
    inv = raw.get("inventory_proof")
    if not isinstance(inv, dict):
        return
    proof = inv.pop("proof", None)
    if isinstance(proof, dict):
        inv.update(proof)


def _nest_inventory_proof(raw: dict[str, Any]) -> None:
    inv = raw.get("inventory_proof")
    if not isinstance(inv, dict):
        return
    proof = {}
    for key in (
        "ots_path",
        "verified",
        "block_height",
        "confirmations_at_verify",
        "verified_at",
    ):
        if key in inv:
            proof[key] = inv.pop(key)
    inv["proof"] = proof


def _omit_absent_batch_fields(raw: dict[str, Any]) -> None:
    submissions = raw.get("submissions")
    if isinstance(submissions, list | tuple):
        for submission in submissions:
            if isinstance(submission, dict):
                _pop_none(submission, "batch_id")
                _pop_none(submission, "record_ids")

    inv = raw.get("inventory_proof")
    if isinstance(inv, dict):
        _pop_none(inv, "batch_id")
        _pop_none(inv, "record_id")


def _pop_none(raw: dict[str, Any], key: str) -> None:
    if raw.get(key) is None:
        raw.pop(key, None)
