"""ots.json checkpoint: load_ots / save_ots / migrations / helpers / drift.

Pins:
- atomic write-temp + fsync + os.replace
- schema_version=1 round-trip; >current => StagingError; migrations chain
- bare-hex digest convention (no `sha256:` prefix on disk)
- crash-safe submit intent durability ("submitting" entry persisted before POST)
- helpers: ots_cache_path, plan_path, has_ots, remove_ots
- category_id drift: WARN log + return persisted value
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from ocfl_cli import staging as st_mod
from ocfl_cli.errors import StagingError
from ocfl_cli.staging import (
    InventoryProof,
    OtsState,
    Phase,
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

        # Pretend current is v1 (it is); register a v0->v1 step.
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
        params = Submission.__dataclass_params__
        assert params.frozen is True

    def test_verify_job_frozen(self):
        params = VerifyJob.__dataclass_params__
        assert params.frozen is True

    def test_verified_proof_frozen(self):
        params = VerifiedProof.__dataclass_params__
        assert params.frozen is True

    def test_submission_digests_is_tuple(self):
        s = Submission(
            submitted_at="2026-04-23T16:30:00+00:00",
            digests=(_bare_hex("a"),),
            status="submitted",
            job_id="j",
            attempts=1,
        )
        assert isinstance(s.digests, tuple)
