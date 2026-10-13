"""Tests for ocfl stage discard."""

from pathlib import Path

import pytest
from msgspec.structs import replace
from typer.testing import CliRunner

from ocfl_cli.main import app
from ocfl_cli.model.ots_state import (
    OtsState,
    Proof,
    StagedInventory,
    Submission,
    VerifyJob,
)
from ocfl_cli.model.phase import Phase, TimestampTarget
from ocfl_cli.store.ots_state import has_ots, load_ots, ots_cache_path, save_ots
from ocfl_cli.store.staging import StagingStore


@pytest.fixture
def runner():
    return CliRunner()


def _invoke(runner, storage_root, staging, *args):
    return runner.invoke(
        app,
        ["--root", str(storage_root), "stage", "--staging-root", str(staging), *args],
    )


class TestStageDiscard:
    def test_discard_clears_staging(self, runner, storage_root, tmp_path):
        staging = tmp_path / "staging"
        _invoke(runner, storage_root, staging, "new", "obj:new")

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        assert store.exists(root_path, "obj:new")

        result = _invoke(runner, storage_root, staging, "discard", "obj:new")
        assert result.exit_code == 0, result.output
        assert not store.exists(root_path, "obj:new")

    def test_discard_no_staging_warns(self, runner, storage_root, tmp_path):
        """Discard on non-existent staging warns and exits 0."""
        staging = tmp_path / "staging"
        result = _invoke(runner, storage_root, staging, "discard", "obj:nope")
        assert result.exit_code == 0
        # Should have a warning
        assert "no staging" in result.output.lower() or "warn" in result.output.lower()


def _staging_dir(storage_root: Path, staging: Path, object_id: str) -> Path:
    return StagingStore(staging).staging_path(str(storage_root.resolve()), object_id)


def _mk_ots_staging(
    runner: CliRunner,
    storage_root: Path,
    staging: Path,
    object_id: str,
) -> Path:
    """Create a basic staging area + write `ots.json` at fresh drafting.

    Side-steps the [simpleproof]/[bitcoin] preflight that `stage new --ots`
    runs by creating a non-OTS staging then dropping `ots.json` directly.
    Sufficient because the discard command only inspects `ots.json` shape.
    """
    r = _invoke(runner, storage_root, staging, "new", object_id)
    assert r.exit_code == 0, r.output
    sd = _staging_dir(storage_root, staging, object_id)
    save_ots(sd, OtsState(phase=Phase.DRAFTING, category_id=641))
    assert has_ots(sd)
    return sd


def _verified_proof(digest: str = "deadbeef" * 8) -> Proof:
    return Proof(
        target=TimestampTarget.CONTENT,
        ots_path=f"ots-cache/{digest}.ots",
        verified=True,
        block_height=800_000,
        confirmations_at_verify=3,
        verified_at="2026-04-23T16:30:00+00:00",
    )


def _inv_proof(version: str = "v1", digest: str = "feedface" * 8) -> StagedInventory:
    return StagedInventory(version=version, digest=digest, job_id="job-inv")


INV_DIGEST = "feedface" * 8


def _inv_proof_entry() -> Proof:
    return replace(_verified_proof(INV_DIGEST), target=TimestampTarget.INVENTORY)


def _submission(
    digest: str,
    *,
    status: str = "submitted",
    batch_id: str | None = None,
    target: TimestampTarget = TimestampTarget.CONTENT,
) -> Submission:
    return Submission(
        submitted_at="2026-04-25T14:00:00+00:00",
        digests=(digest,),
        status=status,
        target=target,
        job_id="job-1",
        attempts=1,
        batch_id=batch_id,
    )


def _verify_job(digest: str) -> VerifyJob:
    return VerifyJob(job_id="job-1", digests=(digest,), status="IN_PROGRESS")


def _pin_state(sd: Path, phase: Phase, **fields: object) -> OtsState:
    state = load_ots(sd)
    state = replace(state, phase=phase, **fields)
    save_ots(sd, state)
    return state


class TestForceRequiredAtNonDraftingPhase:
    @pytest.mark.parametrize(
        ("phase", "fields"),
        [
            (
                Phase.CONTENT_PENDING,
                {
                    "pending_digests": ("a" * 64,),
                    "submissions": (_submission("a" * 64),),
                    "verify_jobs": (_verify_job("a" * 64),),
                },
            ),
            (
                Phase.CONTENT_UPGRADED,
                {
                    "pending_digests": ("a" * 64,),
                    "submissions": (_submission("a" * 64),),
                    "proofs": {"a" * 64: _verified_proof("a" * 64)},
                },
            ),
            (
                Phase.INVENTORY_PENDING,
                {
                    "submissions": (_submission("a" * 64), _submission("b" * 64)),
                    "proofs": {
                        "a" * 64: _verified_proof("a" * 64),
                        INV_DIGEST: _inv_proof_entry(),
                    },
                    "inventory": _inv_proof(),
                },
            ),
            (
                Phase.INVENTORY_UPGRADED,
                {
                    "proofs": {
                        "a" * 64: _verified_proof("a" * 64),
                        INV_DIGEST: _inv_proof_entry(),
                    },
                    "inventory": _inv_proof(),
                },
            ),
        ],
        ids=[
            "content-pending",
            "content-upgraded",
            "inventory-pending",
            "inventory-upgraded",
        ],
    )
    def test_discard_without_force_refuses(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        phase: Phase,
        fields: dict[str, object],
    ) -> None:
        staging = tmp_path / "staging"
        sd = _mk_ots_staging(runner, storage_root, staging, "obj:gated")
        _pin_state(sd, phase, **fields)

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        assert store.exists(root_path, "obj:gated")

        r = _invoke(runner, storage_root, staging, "discard", "obj:gated")

        assert r.exit_code != 0, (
            f"discard at phase {phase.value!r} with persisted progress "
            f"must require --force; got exit=0 output={r.output!r}"
        )
        assert "--force" in r.output, (
            f"refusal message must hint at --force; got {r.output!r}"
        )
        assert store.exists(root_path, "obj:gated"), "refusal must not purge staging"
        assert load_ots(sd).phase is phase

    @pytest.mark.parametrize(
        ("phase", "fields"),
        [
            (
                Phase.CONTENT_PENDING,
                {
                    "pending_digests": ("a" * 64,),
                    "submissions": (_submission("a" * 64),),
                    "verify_jobs": (_verify_job("a" * 64),),
                },
            ),
            (
                Phase.INVENTORY_UPGRADED,
                {
                    "proofs": {
                        "a" * 64: _verified_proof("a" * 64),
                        INV_DIGEST: _inv_proof_entry(),
                    },
                    "inventory": _inv_proof(),
                },
            ),
        ],
        ids=["content-pending", "inventory-upgraded"],
    )
    def test_discard_with_force_proceeds(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        phase: Phase,
        fields: dict[str, object],
    ) -> None:
        staging = tmp_path / "staging"
        sd = _mk_ots_staging(runner, storage_root, staging, "obj:gated-force")
        _pin_state(sd, phase, **fields)

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())

        r = _invoke(
            runner, storage_root, staging, "discard", "obj:gated-force", "--force"
        )
        assert r.exit_code == 0, r.output
        assert not store.exists(root_path, "obj:gated-force"), (
            "discard --force must purge staging at any phase"
        )


class TestForceRequiredAtDraftingAfterRegression:
    def test_force_required_when_drafting_has_preserved_proofs(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
    ) -> None:
        staging = tmp_path / "staging"
        sd = _mk_ots_staging(runner, storage_root, staging, "obj:regressed")

        # Mirrors post-rm/mv regression shape: phase=drafting but
        # proofs preserved — that still counts as progress.
        da = "a" * 64
        _pin_state(
            sd,
            Phase.DRAFTING,
            pending_digests=(da,),
            proofs={da: _verified_proof(da)},
        )

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())

        r = _invoke(runner, storage_root, staging, "discard", "obj:regressed")
        assert r.exit_code != 0, (
            "drafting + preserved proofs must still require --force; "
            f"got exit=0 output={r.output!r}"
        )
        assert "--force" in r.output
        assert store.exists(root_path, "obj:regressed")

        r2 = _invoke(
            runner, storage_root, staging, "discard", "obj:regressed", "--force"
        )
        assert r2.exit_code == 0, r2.output
        assert not store.exists(root_path, "obj:regressed")


class TestForceNotRequiredAtFreshDrafting:
    def test_fresh_drafting_no_force_needed(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
    ) -> None:
        staging = tmp_path / "staging"
        sd = _mk_ots_staging(runner, storage_root, staging, "obj:fresh")
        s = load_ots(sd)
        assert s.phase is Phase.DRAFTING
        assert s.submissions == ()
        assert s.verify_jobs == ()
        assert s.proofs == {}
        assert s.inventory_proof is None

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())

        r = _invoke(runner, storage_root, staging, "discard", "obj:fresh")
        assert r.exit_code == 0, (
            f"fresh drafting must not require --force; got {r.output!r}"
        )
        assert not store.exists(root_path, "obj:fresh")


class TestDiscardPurgesOtsCache:
    def test_discard_force_purges_ots_cache(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
    ) -> None:
        staging = tmp_path / "staging"
        sd = _mk_ots_staging(runner, storage_root, staging, "obj:cache")

        da = "a" * 64
        _pin_state(
            sd,
            Phase.CONTENT_UPGRADED,
            pending_digests=(da,),
            submissions=(_submission(da),),
            proofs={da: _verified_proof(da)},
        )

        cache = ots_cache_path(sd, da)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(b"cached-ots-bytes")
        cache_dir = sd / "ots-cache"
        assert cache_dir.is_dir() and cache.is_file()

        r = _invoke(runner, storage_root, staging, "discard", "obj:cache", "--force")
        assert r.exit_code == 0, r.output

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        assert not store.exists(root_path, "obj:cache"), (
            "discard --force must purge the staging directory"
        )
        assert not cache_dir.exists(), (
            "discard must purge `ots-cache/` (no leftover cached `.ots` blobs)"
        )
        assert not cache.exists()


class TestDiscardWarnMessage:
    def test_refusal_message_names_active_content_batch_status_command(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
    ) -> None:
        staging = tmp_path / "staging"
        sd = _mk_ots_staging(runner, storage_root, staging, "obj:batch")

        batch_id = "018f1f7e-7b4a-7c6a-8a2b-4a7f4c2e9d10"
        _pin_state(
            sd,
            Phase.CONTENT_PENDING,
            pending_digests=("a" * 64,),
            submissions=(_submission("a" * 64, status="submitted", batch_id=batch_id),),
        )

        r = _invoke(runner, storage_root, staging, "discard", "obj:batch")

        assert r.exit_code != 0, r.output
        assert batch_id in r.output
        assert f"ocfl stage batch status {batch_id}" in r.output

    def test_refusal_message_names_active_inventory_batch_status_command(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
    ) -> None:
        staging = tmp_path / "staging"
        sd = _mk_ots_staging(runner, storage_root, staging, "obj:inventory-batch")

        batch_id = "018f1f7e-7b4a-7c6a-8a2b-4a7f4c2e9d10"
        _pin_state(
            sd,
            Phase.INVENTORY_PENDING,
            inventory=_inv_proof(),
            proofs={INV_DIGEST: _inv_proof_entry()},
            # The inventory's batch lives on its Submission, like content's.
            submissions=(
                _submission(
                    INV_DIGEST, batch_id=batch_id, target=TimestampTarget.INVENTORY
                ),
            ),
        )

        r = _invoke(runner, storage_root, staging, "discard", "obj:inventory-batch")

        assert r.exit_code != 0, r.output
        assert batch_id in r.output
        assert f"ocfl stage batch status {batch_id}" in r.output

    def test_warn_message_names_phase_and_counts(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
    ) -> None:
        staging = tmp_path / "staging"
        sd = _mk_ots_staging(runner, storage_root, staging, "obj:warn")

        da, db = "a" * 64, "b" * 64
        _pin_state(
            sd,
            Phase.INVENTORY_PENDING,
            pending_digests=(da, db),
            submissions=(_submission(da), _submission(db)),
            verify_jobs=(_verify_job(da),),
            proofs={
                da: _verified_proof(da),
                db: _verified_proof(db),
                INV_DIGEST: _inv_proof_entry(),
            },
            inventory=_inv_proof(),
        )

        r = _invoke(runner, storage_root, staging, "discard", "obj:warn", "--force")
        assert r.exit_code == 0, r.output

        out = r.output
        assert Phase.INVENTORY_PENDING.value in out, (
            f"warn message must name the discarded phase "
            f"({Phase.INVENTORY_PENDING.value!r}); got {out!r}"
        )
        for label, count in (
            ("submissions", 2),
            ("verify_jobs", 1),
            ("proofs", 3),
        ):
            assert str(count) in out, (
                f"warn message must include count={count} for {label}; got {out!r}"
            )
        lower = out.lower()
        assert "submission" in lower
        assert "verify" in lower
        assert "proofs=3" in lower


class TestNonOtsDiscardUnchanged:
    def test_non_ots_discard_does_not_require_force(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
    ) -> None:
        staging = tmp_path / "staging"
        r = _invoke(runner, storage_root, staging, "new", "obj:plain")
        assert r.exit_code == 0, r.output

        sd = _staging_dir(storage_root, staging, "obj:plain")
        assert not has_ots(sd), "non-OTS staging must not have ots.json"

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        assert store.exists(root_path, "obj:plain")

        r = _invoke(runner, storage_root, staging, "discard", "obj:plain")
        assert r.exit_code == 0, r.output
        assert not store.exists(root_path, "obj:plain")
        assert "--force" not in r.output
