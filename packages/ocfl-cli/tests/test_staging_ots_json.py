"""ots.json checkpoint: load_ots / save_ots / migrations / helpers / drift.

Pins:
- atomic write-temp + fsync + os.replace
- schema_version=1 round-trip; >current => StagingError; migrations chain
- bare-hex digest convention (no `sha256:` prefix on disk)
- crash-safe submit intent durability ("submitting" entry persisted before POST)
- helpers: ots_cache_path, plan_path, has_ots, remove_ots
- category_id drift: WARN log + return persisted value
"""

import json
import logging
import os
from pathlib import Path
from typing import cast

import pytest

from ocfl_cli.errors import StagingError
from ocfl_cli.ots.simpleproof import JobStatus, SimpleProofClient
from ocfl_cli.stage import state as st_mod
from ocfl_cli.stage.fsm import Phase
from ocfl_cli.stage.service import _poll_outstanding_jobs
from ocfl_cli.stage.state import (
    InventoryProof,
    OtsState,
    Submission,
    VerifiedProof,
    VerifyJob,
    apply_category_id,
    has_ots,
    load_ots,
    now_utc_iso,
    ots_cache_path,
    plan_path,
    remove_ots,
    save_ots,
)


def _bare_hex(prefix: str) -> str:
    """Return a 64-char lowercase hex digest derived from a prefix."""
    return (prefix * 64)[:64].lower()


@pytest.fixture
def empty_state() -> OtsState:
    return OtsState(
        schema_version=1,
        phase=Phase.DRAFTING,
        category_id=641,
        pending_digests=(),
        submissions=(),
        verify_jobs=(),
        content_proofs={},
        inventory_proof=None,
    )


@pytest.fixture
def populated_state() -> OtsState:
    d1 = _bare_hex("a")
    d2 = _bare_hex("b")
    return OtsState(
        schema_version=1,
        phase=Phase.CONTENT_PENDING,
        category_id=641,
        pending_digests=(d1, d2),
        submissions=(
            Submission(
                submitted_at="2026-04-23T16:30:00+00:00",
                digests=(d1, d2),
                status="submitted",
                job_id="job-1",
                attempts=1,
            ),
        ),
        verify_jobs=(VerifyJob(job_id="vj-1", digests=(d1,), status="IN_PROGRESS"),),
        content_proofs={
            d1: VerifiedProof(
                ots_path=f"ots-cache/{d1}.ots",
                verified=True,
                block_height=923451,
                block_hash="00" * 32,
                confirmations_at_verify=3,
                verified_at="2026-04-23T16:45:12+00:00",
            ),
            d2: VerifiedProof(
                ots_path=None,
                verified=False,
                block_height=None,
                confirmations_at_verify=None,
                verified_at=None,
            ),
        },
        inventory_proof=None,
    )


class TestRoundTrip:
    def test_empty_round_trip(self, tmp_path: Path, empty_state: OtsState):
        save_ots(tmp_path, empty_state)
        loaded = load_ots(tmp_path)
        assert loaded == empty_state
        raw = json.loads((tmp_path / "ots.json").read_bytes())
        assert raw == {
            "schema_version": 1,
            "phase": "drafting",
            "category_id": 641,
            "pending_digests": [],
            "submissions": [],
            "verify_jobs": [],
            "content_proofs": {},
            "inventory_proof": None,
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
        proof = VerifiedProof(
            ots_path=f"ots-cache/{inv_d}.ots",
            verified=True,
            block_height=923500,
            block_hash="11" * 32,
            confirmations_at_verify=3,
            verified_at="2026-04-23T17:02:08+00:00",
        )
        state = OtsState(
            schema_version=1,
            phase=Phase.INVENTORY_UPGRADED,
            category_id=641,
            pending_digests=(),
            submissions=(),
            verify_jobs=(),
            content_proofs={},
            inventory_proof=InventoryProof(
                version="v3",
                digest=inv_d,
                proof=proof,
                job_id="job-inv-1",
            ),
        )
        save_ots(tmp_path, state)
        loaded = load_ots(tmp_path)
        assert loaded == state
        assert loaded.inventory_proof is not None
        assert loaded.inventory_proof.digest == inv_d
        assert loaded.inventory_proof.proof.verified is True
        assert loaded.inventory_proof.proof.block_height == 923500
        assert loaded.inventory_proof.proof.block_hash == "11" * 32
        raw = json.loads((tmp_path / "ots.json").read_bytes())
        assert raw["inventory_proof"] == {
            "version": "v3",
            "digest": inv_d,
            "job_id": "job-inv-1",
            "ots_path": f"ots-cache/{inv_d}.ots",
            "verified": True,
            "block_height": 923500,
            "block_hash": "11" * 32,
            "confirmations_at_verify": 3,
            "verified_at": "2026-04-23T17:02:08+00:00",
        }
        assert "proof" not in raw["inventory_proof"]


class TestTypedDecoding:
    def test_unknown_fields_are_ignored_for_forward_compat(self, tmp_path: Path):
        digest = _bare_hex("a")
        (tmp_path / "ots.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "phase": "inventory-pending",
                    "category_id": 641,
                    "pending_digests": [],
                    "submissions": [],
                    "verify_jobs": [],
                    "content_proofs": {
                        digest: {
                            "ots_path": f"ots-cache/{digest}.ots",
                            "verified": True,
                            "block_height": 923451,
                            "confirmations_at_verify": 3,
                            "verified_at": "2026-04-23T16:45:12+00:00",
                            "future_proof_field": "ignored",
                        }
                    },
                    "inventory_proof": {
                        "version": "v3",
                        "digest": digest,
                        "job_id": "job-inv-1",
                        "verified": False,
                        "future_inventory_field": "ignored",
                    },
                    "future_top_level_field": "ignored",
                }
            )
        )

        state = load_ots(tmp_path)

        assert state.content_proofs[digest].verified is True
        assert state.inventory_proof is not None
        assert state.inventory_proof.digest == digest

    def test_load_warns_on_unknown_verify_job_status(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        digest = _bare_hex("a")
        (tmp_path / "ots.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
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
                    "content_proofs": {},
                    "inventory_proof": None,
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
            schema_version=1,
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
            schema_version=1,
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

        polled = _poll_outstanding_jobs(state, cast(SimpleProofClient, poller))

        assert poller.job_ids == ["job-new-status"]
        assert polled.verify_jobs[0].status == "IN_PROGRESS"

    def test_rejects_string_submission_digests(self, tmp_path: Path):
        (tmp_path / "ots.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "phase": "content-pending",
                    "category_id": 641,
                    "pending_digests": [],
                    "submissions": [
                        {
                            "submitted_at": "2026-04-23T16:30:00+00:00",
                            "digests": "abc",
                            "status": "submitted",
                            "job_id": "job-1",
                            "attempts": 1,
                        }
                    ],
                    "verify_jobs": [],
                    "content_proofs": {},
                    "inventory_proof": None,
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
                    "schema_version": 1,
                    "phase": "content-pending",
                    "category_id": 641,
                    "pending_digests": [],
                    "submissions": [],
                    "verify_jobs": [],
                    "content_proofs": {
                        digest: {
                            "ots_path": f"ots-cache/{digest}.ots",
                            "verified": "yes",
                            "block_height": 923451,
                            "confirmations_at_verify": 3,
                            "verified_at": "2026-04-23T16:45:12+00:00",
                        }
                    },
                    "inventory_proof": None,
                }
            )
        )

        with pytest.raises(StagingError, match="content_proofs"):
            load_ots(tmp_path)

    def test_rejects_string_inventory_verified_flag(self, tmp_path: Path):
        digest = _bare_hex("c")
        (tmp_path / "ots.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "phase": "inventory-upgraded",
                    "category_id": 641,
                    "pending_digests": [],
                    "submissions": [],
                    "verify_jobs": [],
                    "content_proofs": {},
                    "inventory_proof": {
                        "version": "v3",
                        "digest": digest,
                        "job_id": "job-inv-1",
                        "ots_path": f"ots-cache/{digest}.ots",
                        "verified": "true",
                        "block_height": 923500,
                        "confirmations_at_verify": 3,
                        "verified_at": "2026-04-23T17:02:08+00:00",
                    },
                }
            )
        )

        with pytest.raises(StagingError, match="inventory_proof"):
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

    def test_content_proofs_keys_bare_hex(
        self, tmp_path: Path, populated_state: OtsState
    ):
        save_ots(tmp_path, populated_state)
        raw = json.loads((tmp_path / "ots.json").read_bytes())
        for k in raw["content_proofs"]:
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

        # Reader still sees the old, intact state.
        assert load_ots(tmp_path) == empty_state
        leftover = [p for p in tmp_path.iterdir() if ".tmp" in p.name]
        assert leftover == []

    def test_load_never_torn(
        self, tmp_path: Path, empty_state: OtsState, populated_state: OtsState
    ):
        """Whatever load_ots returns must be a fully-formed OtsState (or raise)
        — never a half-empty / mixed shape."""
        save_ots(tmp_path, empty_state)
        save_ots(tmp_path, populated_state)
        loaded = load_ots(tmp_path)
        assert loaded == populated_state


class TestSchemaMigration:
    def test_constants(self):
        assert st_mod._OTS_STATE_VERSION == 1
        assert st_mod._MIGRATIONS == {}

    def test_v1_loads_cleanly(self, tmp_path: Path):
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
        state = load_ots(tmp_path)
        assert state.schema_version == 1
        assert state.phase is Phase.DRAFTING

    def test_v1_submission_and_inventory_batch_fields_default_absent(
        self, tmp_path: Path
    ):
        digest = _bare_hex("a")
        (tmp_path / "ots.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "phase": "inventory-pending",
                    "category_id": 641,
                    "pending_digests": [digest],
                    "submissions": [
                        {
                            "submitted_at": "2026-04-23T16:30:00+00:00",
                            "digests": [digest],
                            "status": "submitted",
                            "job_id": "job-1",
                            "attempts": 1,
                        }
                    ],
                    "verify_jobs": [],
                    "content_proofs": {},
                    "inventory_proof": {
                        "version": "v3",
                        "digest": digest,
                        "job_id": "job-inv-1",
                        "verified": False,
                    },
                }
            )
        )

        state = load_ots(tmp_path)

        assert state.schema_version == 1
        assert state.submissions[0].batch_id is None
        assert state.submissions[0].record_ids is None
        assert state.inventory_proof is not None
        assert state.inventory_proof.batch_id is None
        assert state.inventory_proof.record_id is None

        save_ots(tmp_path, state)
        raw = json.loads((tmp_path / "ots.json").read_bytes())
        assert "batch_id" not in raw["submissions"][0]
        assert "record_ids" not in raw["submissions"][0]
        assert "batch_id" not in raw["inventory_proof"]
        assert "record_id" not in raw["inventory_proof"]
        assert load_ots(tmp_path) == state

    def test_newer_version_rejected(self, tmp_path: Path):
        (tmp_path / "ots.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
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
        with pytest.raises(StagingError):
            load_ots(tmp_path)

    def test_migration_chain_walks_old_forward(self, tmp_path: Path, monkeypatch):
        """Future-bump pattern: register a fake _MIGRATIONS[0] to walk v0 → v1."""

        def v0_to_v1(raw: dict) -> dict:
            # Pretend v0 lacked verify_jobs; populate it and bump.
            new = dict(raw)
            new.setdefault("verify_jobs", [])
            return new

        # Register a v0->v1 step; the built-in v1->v2 step finishes the walk.
        monkeypatch.setitem(st_mod._MIGRATIONS, 0, v0_to_v1)

        (tmp_path / "ots.json").write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "phase": "drafting",
                    "category_id": 641,
                    "pending_digests": [],
                    "submissions": [],
                    # verify_jobs missing — migration adds it
                    "content_proofs": {},
                    "inventory_proof": None,
                }
            )
        )

        state = load_ots(tmp_path)
        assert state.schema_version == 1
        assert state.verify_jobs == ()


class TestSubmitIntentDurability:
    """Crash-safe submit protocol: 'submitting' status durable on disk before POST."""

    def test_submitting_entry_persists(self, tmp_path: Path):
        d1 = _bare_hex("a")
        intent = OtsState(
            schema_version=1,
            phase=Phase.CONTENT_PENDING,
            category_id=641,
            pending_digests=(d1,),
            submissions=(
                Submission(
                    submitted_at="2026-04-23T16:30:00+00:00",
                    digests=(d1,),
                    status="submitting",
                    job_id=None,
                    attempts=0,
                ),
            ),
            verify_jobs=(),
            content_proofs={},
            inventory_proof=None,
        )
        save_ots(tmp_path, intent)
        loaded = load_ots(tmp_path)
        assert loaded.submissions[0].status == "submitting"
        assert loaded.submissions[0].job_id is None

    def test_intent_to_finalize_two_step(self, tmp_path: Path):
        """Step 1: write intent. Step 2: finalize. Reader at any moment sees one
        valid OtsState — never a torn file."""
        d1 = _bare_hex("a")
        intent = OtsState(
            schema_version=1,
            phase=Phase.CONTENT_PENDING,
            category_id=641,
            pending_digests=(d1,),
            submissions=(
                Submission(
                    submitted_at="2026-04-23T16:30:00+00:00",
                    digests=(d1,),
                    status="submitting",
                    job_id=None,
                    attempts=0,
                ),
            ),
            verify_jobs=(),
            content_proofs={},
            inventory_proof=None,
        )
        save_ots(tmp_path, intent)
        loaded_a = load_ots(tmp_path)
        assert loaded_a == intent

        finalized = OtsState(
            schema_version=1,
            phase=Phase.CONTENT_PENDING,
            category_id=641,
            pending_digests=(d1,),
            submissions=(
                Submission(
                    submitted_at="2026-04-23T16:30:00+00:00",
                    digests=(d1,),
                    status="submitted",
                    job_id="job-x",
                    attempts=1,
                ),
            ),
            verify_jobs=(),
            content_proofs={},
            inventory_proof=None,
        )
        save_ots(tmp_path, finalized)
        loaded_b = load_ots(tmp_path)
        assert loaded_b == finalized
        # Disk shape post-finalize is consistent — never half-old / half-new.
        raw = json.loads((tmp_path / "ots.json").read_bytes())
        assert raw["submissions"][0]["status"] == "submitted"
        assert raw["submissions"][0]["job_id"] == "job-x"


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
        # Should not raise on missing file.
        remove_ots(tmp_path)
        assert not has_ots(tmp_path)

    def test_ots_cache_path(self, tmp_path: Path):
        d = _bare_hex("a")
        assert ots_cache_path(tmp_path, d) == tmp_path / "ots-cache" / f"{d}.ots"

    def test_plan_path(self, tmp_path: Path):
        assert plan_path(tmp_path) == tmp_path / "plan.json"

    def test_now_utc_iso_format(self):
        s = now_utc_iso()
        assert s.endswith("+00:00")
        # No microseconds (timespec="seconds"): "YYYY-MM-DDTHH:MM:SS+00:00"
        assert len(s) == 25


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
    """Submissions / VerifyJobs / VerifiedProof / InventoryProof are frozen
    sub-records. Their `digests` field is always a tuple — submission rounds
    are immutable once recorded."""

    def test_submission_frozen(self):
        s = Submission(
            submitted_at="2026-04-23T16:30:00+00:00",
            digests=(_bare_hex("a"),),
            status="submitted",
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
        proof = VerifiedProof(verified=False)
        attr = "verified"
        with pytest.raises(AttributeError):
            setattr(proof, attr, True)

    def test_submission_digests_is_tuple(self):
        s = Submission(
            submitted_at="2026-04-23T16:30:00+00:00",
            digests=(_bare_hex("a"),),
            status="submitted",
            job_id="j",
            attempts=1,
        )
        assert isinstance(s.digests, tuple)
