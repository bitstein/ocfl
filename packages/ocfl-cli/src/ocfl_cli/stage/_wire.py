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
