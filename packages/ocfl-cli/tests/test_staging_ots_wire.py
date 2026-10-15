import copy

import pytest

from ocfl_cli.errors import StagingError
from ocfl_cli.model.ots_state import OtsState, Proof, StagedInventory, Submission
from ocfl_cli.model.phase import Phase, TimestampTarget
from ocfl_cli.store._wire import decode_ots_state, encode_ots_state


def _bare_hex(prefix: str) -> str:
    return (prefix * 64)[:64].lower()


def test_encode_keeps_proofs_in_one_table() -> None:
    digest = _bare_hex("a")
    state = OtsState(
        phase=Phase.INVENTORY_UPGRADED,
        category_id=641,
        inventory=StagedInventory(version="v3", digest=digest, job_id="job-1"),
        proofs={
            digest: Proof(
                target=TimestampTarget.INVENTORY,
                ots_path=f"ots-cache/{digest}.ots",
                verified=True,
                block_height=923500,
                confirmations_at_verify=3,
                verified_at="2026-04-23T17:02:08+00:00",
            )
        },
    )

    raw = encode_ots_state(state)

    assert raw["inventory"] == {"version": "v3", "digest": digest, "job_id": "job-1"}
    assert raw["proofs"][digest] == {
        "target": "inventory",
        "ots_path": f"ots-cache/{digest}.ots",
        "verified": True,
        "block_height": 923500,
        "confirmations_at_verify": 3,
        "verified_at": "2026-04-23T17:02:08+00:00",
    }
    assert "content_proofs" not in raw and "inventory_proof" not in raw
    assert raw["schema_version"] == 2


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
                target=TimestampTarget.CONTENT,
            ),
        ),
        inventory=StagedInventory(version="v3", digest=digest),
    )

    raw = encode_ots_state(state)

    assert "batch_id" not in raw["submissions"][0]
    assert "record_ids" not in raw["submissions"][0]
    assert raw["submissions"][0]["target"] == "content"
    assert raw["inventory"] == {"version": "v3", "digest": digest, "job_id": None}


def test_batch_fields_round_trip_when_present() -> None:
    digest = _bare_hex("b")
    inv = _bare_hex("c")
    state = OtsState(
        phase=Phase.CONTENT_PENDING,
        category_id=641,
        submissions=(
            Submission(
                submitted_at="2026-04-23T16:30:00+00:00",
                digests=(digest,),
                status="submitted",
                target=TimestampTarget.CONTENT,
                batch_id="batch-1",
                record_ids=("record-1",),
            ),
            Submission(
                submitted_at="2026-04-23T16:40:00+00:00",
                digests=(inv,),
                status="submitted",
                target=TimestampTarget.INVENTORY,
                batch_id="batch-2",
                record_ids=("record-2",),
            ),
        ),
        inventory=StagedInventory(version="v3", digest=inv),
    )

    raw = encode_ots_state(state)
    decoded = decode_ots_state(raw)

    assert raw["submissions"][0]["batch_id"] == "batch-1"
    assert raw["submissions"][0]["record_ids"] == ("record-1",)
    assert raw["submissions"][1]["target"] == "inventory"
    assert raw["submissions"][1]["batch_id"] == "batch-2"
    assert raw["submissions"][1]["record_ids"] == ("record-2",)
    assert raw["inventory"] == {"version": "v3", "digest": inv, "job_id": None}
    assert decoded == state


def test_decode_reads_the_inventory_proof_from_the_table() -> None:
    digest = _bare_hex("b")
    state = decode_ots_state(
        {
            "schema_version": 2,
            "phase": "inventory-pending",
            "category_id": 641,
            "pending_digests": [],
            "submissions": [],
            "verify_jobs": [],
            "proofs": {
                digest: {
                    "target": "inventory",
                    "ots_path": f"ots-cache/{digest}.ots",
                    "verified": True,
                    "block_height": 923500,
                    "confirmations_at_verify": 3,
                    "verified_at": "2026-04-23T17:02:08+00:00",
                }
            },
            "inventory": {"version": "v3", "digest": digest, "job_id": "job-1"},
        }
    )

    assert state.inventory is not None
    assert state.inventory.digest == digest
    proof = state.inventory_proof
    assert proof is not None
    assert proof.target is TimestampTarget.INVENTORY
    assert proof.ots_path == f"ots-cache/{digest}.ots"
    assert proof.verified is True


def test_decode_accepts_hypothetical_migration_output_without_mutating_it() -> None:
    digest = _bare_hex("c")
    v2_raw = {
        "schema_version": 2,
        "phase": "inventory-pending",
        "category_id": 641,
        "pending_digests": [],
        "submissions": [],
        "verify_jobs": [],
        "proofs": {
            digest: {
                "target": "inventory",
                "ots_path": f"ots-cache/{digest}.ots",
                "verified": False,
            }
        },
        "inventory": {"version": "v3", "digest": digest, "job_id": "job-1"},
    }

    def migrate_v2_to_v3(raw: dict) -> dict:
        migrated = copy.deepcopy(raw)
        migrated["schema_version"] = 3
        migrated["future_wire_field"] = "ignored"
        return migrated

    migrated_raw = migrate_v2_to_v3(v2_raw)
    original = copy.deepcopy(migrated_raw)

    state = decode_ots_state(migrated_raw)

    assert state.schema_version == 3
    assert state.inventory_proof is not None
    assert state.inventory_proof.ots_path == f"ots-cache/{digest}.ots"
    assert migrated_raw == original


def test_decode_wraps_msgspec_validation_errors() -> None:
    with pytest.raises(StagingError, match="invalid OTS state"):
        decode_ots_state(
            {
                "schema_version": 2,
                "phase": "content-pending",
                "category_id": 641,
                "pending_digests": [],
                "submissions": [{"digests": "not-a-list"}],
                "verify_jobs": [],
                "proofs": {},
                "inventory": None,
            }
        )


def test_decode_rejects_a_proof_without_target() -> None:
    with pytest.raises(StagingError, match="invalid OTS state"):
        decode_ots_state(
            {
                "schema_version": 2,
                "phase": "content-pending",
                "category_id": 641,
                "proofs": {_bare_hex("d"): {"verified": True}},
            }
        )
