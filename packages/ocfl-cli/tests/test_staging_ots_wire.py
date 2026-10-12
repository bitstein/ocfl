"""Direct tests for the ots.json wire-shape boundary."""

import copy

import pytest

from ocfl_cli.errors import StagingError
from ocfl_cli.model.ots_state import InventoryProof, OtsState, Submission, VerifiedProof
from ocfl_cli.model.phase import Phase
from ocfl_cli.store._wire import decode_ots_state, encode_ots_state


def _bare_hex(prefix: str) -> str:
    return (prefix * 64)[:64].lower()


def test_encode_flattens_inventory_proof() -> None:
    digest = _bare_hex("a")
    state = OtsState(
        phase=Phase.INVENTORY_UPGRADED,
        category_id=641,
        inventory_proof=InventoryProof(
            version="v3",
            digest=digest,
            job_id="job-1",
            proof=VerifiedProof(
                ots_path=f"ots-cache/{digest}.ots",
                verified=True,
                block_height=923500,
                confirmations_at_verify=3,
                verified_at="2026-04-23T17:02:08+00:00",
            ),
        ),
    )

    raw = encode_ots_state(state)

    assert raw["inventory_proof"] == {
        "version": "v3",
        "digest": digest,
        "job_id": "job-1",
        "ots_path": f"ots-cache/{digest}.ots",
        "verified": True,
        "block_height": 923500,
        "confirmations_at_verify": 3,
        "verified_at": "2026-04-23T17:02:08+00:00",
    }
    assert "proof" not in raw["inventory_proof"]


def test_encode_omits_absent_batch_fields() -> None:
    digest = _bare_hex("a")
    state = OtsState(
        phase=Phase.CONTENT_PENDING,
        category_id=641,
        submissions=(
            Submission(
                submitted_at="2026-04-23T16:30:00+00:00",
                digests=(digest,),
                status="submitted",
            ),
        ),
        inventory_proof=InventoryProof(version="v3", digest=digest),
    )

    raw = encode_ots_state(state)

    assert "batch_id" not in raw["submissions"][0]
    assert "record_ids" not in raw["submissions"][0]
    assert "batch_id" not in raw["inventory_proof"]
    assert "record_id" not in raw["inventory_proof"]


def test_batch_fields_round_trip_when_present() -> None:
    digest = _bare_hex("b")
    state = OtsState(
        phase=Phase.CONTENT_PENDING,
        category_id=641,
        submissions=(
            Submission(
                submitted_at="2026-04-23T16:30:00+00:00",
                digests=(digest,),
                status="submitted",
                batch_id="batch-1",
                record_ids=("record-1",),
            ),
        ),
        inventory_proof=InventoryProof(
            version="v3",
            digest=digest,
            batch_id="batch-1",
            record_id="record-2",
        ),
    )

    raw = encode_ots_state(state)
    decoded = decode_ots_state(raw)

    assert raw["submissions"][0]["batch_id"] == "batch-1"
    assert raw["submissions"][0]["record_ids"] == ("record-1",)
    assert raw["inventory_proof"]["batch_id"] == "batch-1"
    assert raw["inventory_proof"]["record_id"] == "record-2"
    assert decoded == state


def test_decode_nests_inventory_proof() -> None:
    digest = _bare_hex("b")
    state = decode_ots_state(
        {
            "schema_version": 1,
            "phase": "inventory-pending",
            "category_id": 641,
            "pending_digests": [],
            "submissions": [],
            "verify_jobs": [],
            "content_proofs": {},
            "inventory_proof": {
                "version": "v3",
                "digest": digest,
                "job_id": "job-1",
                "ots_path": f"ots-cache/{digest}.ots",
                "verified": True,
                "block_height": 923500,
                "confirmations_at_verify": 3,
                "verified_at": "2026-04-23T17:02:08+00:00",
            },
        }
    )

    assert state.inventory_proof is not None
    assert state.inventory_proof.digest == digest
    assert state.inventory_proof.proof.ots_path == f"ots-cache/{digest}.ots"
    assert state.inventory_proof.proof.verified is True


def test_decode_accepts_hypothetical_migration_output_without_mutating_it() -> None:
    """Migration steps can hand current-shape raw JSON to the wire decoder."""
    digest = _bare_hex("c")
    v1_raw = {
        "schema_version": 1,
        "phase": "inventory-pending",
        "category_id": 641,
        "pending_digests": [],
        "submissions": [],
        "verify_jobs": [],
        "content_proofs": {},
        "inventory_proof": {
            "version": "v3",
            "digest": digest,
            "job_id": "job-1",
            "ots_path": f"ots-cache/{digest}.ots",
            "verified": False,
            "block_height": None,
            "confirmations_at_verify": None,
            "verified_at": None,
        },
    }

    def migrate_v1_to_v2(raw: dict) -> dict:
        migrated = copy.deepcopy(raw)
        migrated["schema_version"] = 2
        migrated["future_wire_field"] = "ignored"
        return migrated

    migrated_raw = migrate_v1_to_v2(v1_raw)
    original = copy.deepcopy(migrated_raw)

    state = decode_ots_state(migrated_raw)

    assert state.schema_version == 2
    assert state.inventory_proof is not None
    assert state.inventory_proof.proof.ots_path == f"ots-cache/{digest}.ots"
    assert migrated_raw == original


def test_decode_wraps_msgspec_validation_errors() -> None:
    with pytest.raises(StagingError, match="invalid OTS state"):
        decode_ots_state(
            {
                "schema_version": 1,
                "phase": "content-pending",
                "category_id": 641,
                "pending_digests": [],
                "submissions": [{"digests": "not-a-list"}],
                "verify_jobs": [],
                "content_proofs": {},
                "inventory_proof": None,
            }
        )
