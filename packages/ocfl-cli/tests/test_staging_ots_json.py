import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from ocfl_cli.errors import StagingError
from ocfl_cli.model.ots_state import (
    OtsState,
    Proof,
    StagedInventory,
    Submission,
    VerifyJob,
)
from ocfl_cli.model.phase import Phase, TimestampTarget
from ocfl_cli.model.simpleproof import JobStatus
from ocfl_cli.ports import SimpleProofClient
from ocfl_cli.store.ots_state import (
    apply_category_id,
    has_ots,
    load_ots,
    ots_cache_path,
    plan_path,
    remove_ots,
    save_ots,
    utc_iso,
)
from ocfl_cli.workflow.common import poll_outstanding_jobs


def _bare_hex(prefix: str) -> str:
    return (prefix * 64)[:64].lower()


@pytest.fixture
def empty_state() -> OtsState:
    return OtsState(
        schema_version=2,
        phase=Phase.DRAFTING,
        category_id=641,
        pending_digests=(),
        submissions=(),
        verify_jobs=(),
        proofs={},
        inventory=None,
    )


@pytest.fixture
def populated_state() -> OtsState:
    d1 = _bare_hex("a")
    d2 = _bare_hex("b")
    return OtsState(
        schema_version=2,
        phase=Phase.CONTENT_PENDING,
        category_id=641,
        pending_digests=(d1, d2),
        submissions=(
            Submission(
                submitted_at="2026-04-23T16:30:00+00:00",
                digests=(d1, d2),
                status="submitted",
                target=TimestampTarget.CONTENT,
                attempts=1,
            ),
        ),
        verify_jobs=(VerifyJob(job_id="vj-1", digests=(d1,), status="IN_PROGRESS"),),
        proofs={
            d1: Proof(
                target=TimestampTarget.CONTENT,
                ots_path=f"ots-cache/{d1}.ots",
                verified=True,
                block_height=923451,
                block_hash="00" * 32,
                confirmations_at_verify=3,
                verified_at="2026-04-23T16:45:12+00:00",
            ),
            d2: Proof(
                target=TimestampTarget.CONTENT,
                ots_path=None,
                verified=False,
                block_height=None,
                confirmations_at_verify=None,
                verified_at=None,
            ),
        },
        inventory=None,
    )


class TestRoundTrip:
    def test_empty_round_trip(self, tmp_path: Path, empty_state: OtsState):
        save_ots(tmp_path, empty_state)
        loaded = load_ots(tmp_path)
        assert loaded == empty_state
        raw = json.loads((tmp_path / "ots.json").read_bytes())
        assert raw == {
            "schema_version": 2,
            "phase": "drafting",
            "category_id": 641,
            "pending_digests": [],
            "submissions": [],
            "verify_jobs": [],
            "proofs": {},
            "inventory": None,
        }

    def test_populated_round_trip(self, tmp_path: Path, populated_state: OtsState):
        save_ots(tmp_path, populated_state)
        loaded = load_ots(tmp_path)
        assert loaded == populated_state

    def test_phase_serialized_as_wire_string(
        self, tmp_path: Path, populated_state: OtsState
    ):
        save_ots(tmp_path, populated_state)
        raw = json.loads((tmp_path / "ots.json").read_bytes())
        assert raw["phase"] == "content-pending"

    def test_inventory_proof_round_trip(self, tmp_path: Path):
        inv_d = _bare_hex("c")
        proof = Proof(
            target=TimestampTarget.INVENTORY,
            ots_path=f"ots-cache/{inv_d}.ots",
            verified=True,
            block_height=923500,
            block_hash="11" * 32,
            confirmations_at_verify=3,
            verified_at="2026-04-23T17:02:08+00:00",
        )
        state = OtsState(
            schema_version=2,
            phase=Phase.INVENTORY_UPGRADED,
            category_id=641,
            pending_digests=(),
            submissions=(),
            verify_jobs=(),
            proofs={inv_d: proof},
            inventory=StagedInventory(version="v3", digest=inv_d, job_id="job-inv-1"),
        )
        save_ots(tmp_path, state)
        loaded = load_ots(tmp_path)
        assert loaded == state
        assert loaded.inventory is not None
        assert loaded.inventory.digest == inv_d
        assert loaded.inventory_proof == proof
        raw = json.loads((tmp_path / "ots.json").read_bytes())
        assert raw["inventory"] == {
            "version": "v3",
            "digest": inv_d,
            "job_id": "job-inv-1",
        }
        assert raw["proofs"][inv_d] == {
            "target": "inventory",
            "ots_path": f"ots-cache/{inv_d}.ots",
            "verified": True,
            "block_height": 923500,
            "block_hash": "11" * 32,
            "confirmations_at_verify": 3,
            "verified_at": "2026-04-23T17:02:08+00:00",
        }


class TestTypedDecoding:
    def test_unknown_fields_are_ignored_for_forward_compat(self, tmp_path: Path):
        digest = _bare_hex("a")
        (tmp_path / "ots.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "phase": "inventory-pending",
                    "category_id": 641,
                    "pending_digests": [],
                    "submissions": [],
                    "verify_jobs": [],
                    "proofs": {
                        digest: {
                            "target": "content",
                            "ots_path": f"ots-cache/{digest}.ots",
                            "verified": True,
                            "block_height": 923451,
                            "confirmations_at_verify": 3,
                            "verified_at": "2026-04-23T16:45:12+00:00",
                            "future_proof_field": "ignored",
                        }
                    },
                    "inventory": {
                        "version": "v3",
                        "digest": digest,
                        "job_id": "job-inv-1",
                        "future_inventory_field": "ignored",
                    },
                    "future_top_level_field": "ignored",
                }
            )
        )

        state = load_ots(tmp_path)

        assert state.proofs[digest].verified is True
        assert state.inventory is not None
        assert state.inventory.digest == digest

    def test_load_warns_on_unknown_verify_job_status(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        digest = _bare_hex("a")
        (tmp_path / "ots.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "phase": "content-pending",
                    "category_id": 641,
                    "pending_digests": [digest],
                    "submissions": [],
                    "verify_jobs": [
                        {
                            "job_id": "job-new-status",
                            "digests": [digest],
                            "status": "QUEUED",
                        }
                    ],
                    "proofs": {},
                    "inventory": None,
                }
            )
        )

        with caplog.at_level(logging.WARNING):
            state = load_ots(tmp_path)

        assert state.verify_jobs[0].status == "QUEUED"
        assert any(
            "unknown verify job status" in rec.getMessage()
            and "job-new-status" in rec.getMessage()
            and "QUEUED" in rec.getMessage()
            for rec in caplog.records
        )

    def test_expired_verify_job_status_is_canonical(self, tmp_path: Path):
        digest = _bare_hex("a")
        state = OtsState(
            schema_version=2,
            phase=Phase.CONTENT_PENDING,
            category_id=641,
            pending_digests=(digest,),
            verify_jobs=(
                VerifyJob(job_id="job-expired", digests=(digest,), status="EXPIRED"),
            ),
        )

        save_ots(tmp_path, state)
        loaded = load_ots(tmp_path)

        assert loaded.verify_jobs == state.verify_jobs

    def test_pending_verify_job_status_is_canonical(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        digest = _bare_hex("a")
        state = OtsState(
            schema_version=2,
            phase=Phase.CONTENT_PENDING,
            category_id=641,
            pending_digests=(digest,),
            verify_jobs=(
                VerifyJob(job_id="job-pending", digests=(digest,), status="PENDING"),
            ),
        )

        save_ots(tmp_path, state)
        with caplog.at_level(logging.WARNING):
            loaded = load_ots(tmp_path)

        assert loaded.verify_jobs == state.verify_jobs
        assert not any(
            "unknown verify job status" in rec.getMessage() for rec in caplog.records
        )

    def test_unknown_verify_job_status_polls_as_in_progress(self):
        digest = _bare_hex("a")
        state = OtsState(
            phase=Phase.CONTENT_PENDING,
            category_id=641,
            pending_digests=(digest,),
            verify_jobs=(
                VerifyJob(job_id="job-new-status", digests=(digest,), status="QUEUED"),
            ),
        )

        class Poller:
            def __init__(self) -> None:
                self.job_ids: list[str] = []

            def poll_job(self, job_id: str) -> JobStatus:
                self.job_ids.append(job_id)
                return JobStatus(job_id=job_id, status="IN_PROGRESS")

        poller = Poller()

        polled = poll_outstanding_jobs(state, cast(SimpleProofClient, poller))

        assert poller.job_ids == ["job-new-status"]
        assert polled.verify_jobs[0].status == "IN_PROGRESS"

    def test_rejects_string_submission_digests(self, tmp_path: Path):
        (tmp_path / "ots.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "phase": "content-pending",
                    "category_id": 641,
                    "pending_digests": [],
                    "submissions": [
                        {
                            "submitted_at": "2026-04-23T16:30:00+00:00",
                            "digests": "abc",
                            "status": "submitted",
                            "target": "content",
                            "attempts": 1,
                        }
                    ],
                    "verify_jobs": [],
                    "proofs": {},
                    "inventory": None,
                }
            )
        )

        with pytest.raises(StagingError, match="submissions"):
            load_ots(tmp_path)

    def test_rejects_string_verified_flag(self, tmp_path: Path):
        digest = _bare_hex("a")
        (tmp_path / "ots.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "phase": "content-pending",
                    "category_id": 641,
                    "pending_digests": [],
                    "submissions": [],
                    "verify_jobs": [],
                    "proofs": {
                        digest: {
                            "target": "content",
                            "ots_path": f"ots-cache/{digest}.ots",
                            "verified": "yes",
                            "block_height": 923451,
                            "confirmations_at_verify": 3,
                            "verified_at": "2026-04-23T16:45:12+00:00",
                        }
                    },
                    "inventory": None,
                }
            )
        )

        with pytest.raises(StagingError, match="proofs"):
            load_ots(tmp_path)

    def test_rejects_unknown_proof_target(self, tmp_path: Path):
        digest = _bare_hex("c")
        (tmp_path / "ots.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "phase": "inventory-upgraded",
                    "category_id": 641,
                    "proofs": {
                        digest: {"target": "manifest", "verified": True},
                    },
                }
            )
        )

        with pytest.raises(StagingError, match="proofs"):
            load_ots(tmp_path)


class TestBareHexConvention:
    def test_pending_digests_persisted_bare_hex(
        self, tmp_path: Path, populated_state: OtsState
    ):
        save_ots(tmp_path, populated_state)
        raw = json.loads((tmp_path / "ots.json").read_bytes())
        for d in raw["pending_digests"]:
            assert ":" not in d
            assert d == d.lower()
            assert len(d) == 64

    def test_proofs_keys_bare_hex(self, tmp_path: Path, populated_state: OtsState):
        save_ots(tmp_path, populated_state)
        raw = json.loads((tmp_path / "ots.json").read_bytes())
        for k in raw["proofs"]:
            assert ":" not in k
            assert k == k.lower()
            assert len(k) == 64


class TestAtomicWrites:
    def test_save_no_temp_files_left(self, tmp_path: Path, empty_state: OtsState):
        save_ots(tmp_path, empty_state)
        names = sorted(p.name for p in tmp_path.iterdir())
        assert "ots.json" in names
        assert all(".tmp" not in n for n in names)

    def test_save_replace_failure_preserves_old(
        self,
        tmp_path: Path,
        empty_state: OtsState,
        populated_state: OtsState,
        monkeypatch,
    ):
        save_ots(tmp_path, empty_state)

        real_replace = os.replace

        def boom(src, dst, *a, **kw):
            if str(dst).endswith("ots.json"):
                raise OSError("simulated crash")
            return real_replace(src, dst, *a, **kw)

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError, match="simulated crash"):
            save_ots(tmp_path, populated_state)

        assert load_ots(tmp_path) == empty_state
        leftover = [p for p in tmp_path.iterdir() if ".tmp" in p.name]
        assert leftover == []

    def test_load_never_torn(
        self, tmp_path: Path, empty_state: OtsState, populated_state: OtsState
    ):
        save_ots(tmp_path, empty_state)
        save_ots(tmp_path, populated_state)
        loaded = load_ots(tmp_path)
        assert loaded == populated_state


class TestSchemaVersion:
    def test_v2_loads_cleanly(self, tmp_path: Path):
        (tmp_path / "ots.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "phase": "drafting",
                    "category_id": 641,
                    "pending_digests": [],
                    "submissions": [],
                    "verify_jobs": [],
                    "proofs": {},
                    "inventory": None,
                }
            )
        )
        state = load_ots(tmp_path)
        assert state.schema_version == 2
        assert state.phase is Phase.DRAFTING

    def test_older_version_rejected(self, tmp_path: Path):
        (tmp_path / "ots.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "phase": "drafting",
                    "category_id": 641,
                    "pending_digests": [],
                    "submissions": [],
                    "verify_jobs": [],
                    "content_proofs": {},
                    "inventory_proof": None,
                }
            )
        )
        with pytest.raises(StagingError, match="predates CLI schema 2"):
            load_ots(tmp_path)

    def test_submission_batch_fields_default_absent(self, tmp_path: Path):
        digest = _bare_hex("a")
        (tmp_path / "ots.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "phase": "inventory-pending",
                    "category_id": 641,
                    "pending_digests": [digest],
                    "submissions": [
                        {
                            "submitted_at": "2026-04-23T16:30:00+00:00",
                            "digests": [digest],
                            "status": "submitted",
                            "target": "content",
                            "attempts": 1,
                        }
                    ],
                    "verify_jobs": [],
                    "proofs": {},
                    "inventory": {
                        "version": "v3",
                        "digest": digest,
                        "job_id": "job-inv-1",
                    },
                }
            )
        )

        state = load_ots(tmp_path)

        assert state.schema_version == 2
        assert state.submissions[0].batch_id is None
        assert state.submissions[0].record_ids is None
        assert state.inventory is not None
        assert state.inventory.digest == digest

        save_ots(tmp_path, state)
        raw = json.loads((tmp_path / "ots.json").read_bytes())
        assert "batch_id" not in raw["submissions"][0]
        assert "record_ids" not in raw["submissions"][0]
        assert set(raw["inventory"]) == {"version", "digest", "job_id"}
        assert load_ots(tmp_path) == state

    def test_newer_version_rejected(self, tmp_path: Path):
        (tmp_path / "ots.json").write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "phase": "drafting",
                    "category_id": 641,
                    "pending_digests": [],
                    "submissions": [],
                    "verify_jobs": [],
                    "proofs": {},
                    "inventory": None,
                }
            )
        )
        with pytest.raises(StagingError):
            load_ots(tmp_path)


class TestSubmitIntentDurability:
    def test_submitting_entry_persists(self, tmp_path: Path):
        d1 = _bare_hex("a")
        intent = OtsState(
            schema_version=2,
            phase=Phase.CONTENT_PENDING,
            category_id=641,
            pending_digests=(d1,),
            submissions=(
                Submission(
                    submitted_at="2026-04-23T16:30:00+00:00",
                    digests=(d1,),
                    status="submitting",
                    target=TimestampTarget.CONTENT,
                    attempts=0,
                ),
            ),
            verify_jobs=(),
            proofs={},
            inventory=None,
        )
        save_ots(tmp_path, intent)
        loaded = load_ots(tmp_path)
        assert loaded.submissions[0].status == "submitting"

    def test_intent_to_finalize_two_step(self, tmp_path: Path):
        d1 = _bare_hex("a")
        intent = OtsState(
            schema_version=2,
            phase=Phase.CONTENT_PENDING,
            category_id=641,
            pending_digests=(d1,),
            submissions=(
                Submission(
                    submitted_at="2026-04-23T16:30:00+00:00",
                    digests=(d1,),
                    status="submitting",
                    target=TimestampTarget.CONTENT,
                    attempts=0,
                ),
            ),
            verify_jobs=(),
            proofs={},
            inventory=None,
        )
        save_ots(tmp_path, intent)
        loaded_a = load_ots(tmp_path)
        assert loaded_a == intent

        finalized = OtsState(
            schema_version=2,
            phase=Phase.CONTENT_PENDING,
            category_id=641,
            pending_digests=(d1,),
            submissions=(
                Submission(
                    submitted_at="2026-04-23T16:30:00+00:00",
                    digests=(d1,),
                    status="submitted",
                    target=TimestampTarget.CONTENT,
                    attempts=1,
                ),
            ),
            verify_jobs=(),
            proofs={},
            inventory=None,
        )
        save_ots(tmp_path, finalized)
        loaded_b = load_ots(tmp_path)
        assert loaded_b == finalized
        raw = json.loads((tmp_path / "ots.json").read_bytes())
        assert raw["submissions"][0]["status"] == "submitted"


class TestHelpers:
    def test_has_ots_false_when_missing(self, tmp_path: Path):
        assert has_ots(tmp_path) is False

    def test_has_ots_true_after_save(self, tmp_path: Path, empty_state: OtsState):
        save_ots(tmp_path, empty_state)
        assert has_ots(tmp_path) is True

    def test_remove_ots_deletes(self, tmp_path: Path, empty_state: OtsState):
        save_ots(tmp_path, empty_state)
        assert has_ots(tmp_path)
        remove_ots(tmp_path)
        assert not has_ots(tmp_path)

    def test_remove_ots_idempotent(self, tmp_path: Path):
        remove_ots(tmp_path)
        assert not has_ots(tmp_path)

    def test_ots_cache_path(self, tmp_path: Path):
        d = _bare_hex("a")
        assert ots_cache_path(tmp_path, d) == tmp_path / "ots-cache" / f"{d}.ots"

    def test_plan_path(self, tmp_path: Path):
        assert plan_path(tmp_path) == tmp_path / "plan.json"

    def test_utc_iso_format(self):
        s = utc_iso(datetime(2030, 1, 2, 3, 4, 5, 678, tzinfo=UTC))
        assert s == "2030-01-02T03:04:05+00:00"


class TestCategoryIdDrift:
    def test_no_drift_returns_persisted(self, caplog, empty_state: OtsState):
        with caplog.at_level(logging.WARNING):
            result = apply_category_id(empty_state, 641)
        assert result == 641
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warns == []

    def test_drift_logs_warning_and_returns_persisted(
        self, caplog, empty_state: OtsState
    ):
        # empty_state has category_id=641; pass a divergent config value.
        with caplog.at_level(logging.WARNING):
            result = apply_category_id(empty_state, 999)
        assert result == 641
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warns, "expected a WARNING log on category_id drift"
        msg = " ".join(r.getMessage() for r in warns).lower()
        assert "category" in msg


class TestSubmissionShape:
    def test_submission_frozen(self):
        s = Submission(
            submitted_at="2026-04-23T16:30:00+00:00",
            digests=(_bare_hex("a"),),
            status="submitted",
            target=TimestampTarget.CONTENT,
        )
        attr = "status"
        with pytest.raises(AttributeError):
            setattr(s, attr, "failed")

    def test_verify_job_frozen(self):
        job = VerifyJob(job_id="j", digests=(_bare_hex("a"),), status="IN_PROGRESS")
        attr = "status"
        with pytest.raises(AttributeError):
            setattr(job, attr, "COMPLETED")

    def test_verified_proof_frozen(self):
        proof = Proof(target=TimestampTarget.CONTENT, verified=False)
        attr = "verified"
        with pytest.raises(AttributeError):
            setattr(proof, attr, True)

    def test_submission_digests_is_tuple(self):
        s = Submission(
            submitted_at="2026-04-23T16:30:00+00:00",
            digests=(_bare_hex("a"),),
            status="submitted",
            target=TimestampTarget.CONTENT,
            attempts=1,
        )
        assert isinstance(s.digests, tuple)
