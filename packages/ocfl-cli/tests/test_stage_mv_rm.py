"""Tests for ocfl stage mv and ocfl stage rm."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ocfl_cli.main import app
from ocfl_cli.staging import (
    InventoryProof,
    Phase,
    StagingStore,
    VerifiedProof,
    has_ots,
    load_ots,
    ots_cache_path,
    plan_path,
    save_ots,
)


@pytest.fixture
def runner():
    return CliRunner()


def _invoke(runner, storage_root, staging, *args):
    return runner.invoke(
        app,
        ["--root", str(storage_root), "stage", "--staging-root", str(staging), *args],
    )


def _setup_staged(runner, storage_root, tmp_path):
    """Create staging with two files: a.txt and dir/b.txt."""
    staging = tmp_path / "staging"
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "a.txt").write_text("aaa")
    sub = src_dir / "dir"
    sub.mkdir()
    (sub / "b.txt").write_text("bbb")
    (sub / "c.txt").write_text("ccc")

    _invoke(runner, storage_root, staging, "new", "obj:new")
    _invoke(runner, storage_root, staging, "add", "obj:new", str(src_dir))

    store = StagingStore(staging)
    root_path = str(storage_root.resolve())
    return staging, store, root_path


class TestStageMv:
    def test_mv_renames_path(self, runner, storage_root, tmp_path):
        staging, store, root_path = _setup_staged(runner, storage_root, tmp_path)
        result = _invoke(
            runner, storage_root, staging, "mv", "obj:new", "a.txt", "renamed.txt"
        )
        assert result.exit_code == 0, result.output

        inv = store.load_inventory(root_path, "obj:new")
        paths = [p for ps in inv.versions["v1"].state.values() for p in ps]
        assert "renamed.txt" in paths
        assert "a.txt" not in paths

    def test_mv_nonexistent_path_errors(self, runner, storage_root, tmp_path):
        staging, _store, _root_path = _setup_staged(runner, storage_root, tmp_path)
        result = _invoke(
            runner, storage_root, staging, "mv", "obj:new", "nope.txt", "dst.txt"
        )
        assert result.exit_code == 1


class TestStageRm:
    def test_rm_exact_path(self, runner, storage_root, tmp_path):
        staging, store, root_path = _setup_staged(runner, storage_root, tmp_path)
        result = _invoke(runner, storage_root, staging, "rm", "obj:new", "a.txt")
        assert result.exit_code == 0, result.output

        inv = store.load_inventory(root_path, "obj:new")
        paths = [p for ps in inv.versions["v1"].state.values() for p in ps]
        assert "a.txt" not in paths
        assert "dir/b.txt" in paths

    def test_rm_recursive(self, runner, storage_root, tmp_path):
        staging, store, root_path = _setup_staged(runner, storage_root, tmp_path)
        result = _invoke(runner, storage_root, staging, "rm", "obj:new", "dir", "-r")
        assert result.exit_code == 0, result.output

        inv = store.load_inventory(root_path, "obj:new")
        paths = [p for ps in inv.versions["v1"].state.values() for p in ps]
        assert "dir/b.txt" not in paths
        assert "dir/c.txt" not in paths
        assert "a.txt" in paths

    def test_rm_cleans_unreferenced_content(self, runner, storage_root, tmp_path):
        """Content.json entries for unreferenced digests are cleaned."""
        staging, store, root_path = _setup_staged(runner, storage_root, tmp_path)

        # Get digest of a.txt before removing
        inv = store.load_inventory(root_path, "obj:new")
        a_digest = None
        for d, ps in inv.versions["v1"].state.items():
            if "a.txt" in ps:
                a_digest = d
                break
        assert a_digest is not None

        cm_before = store.load_content_map(root_path, "obj:new")
        assert a_digest in cm_before

        _invoke(runner, storage_root, staging, "rm", "obj:new", "a.txt")

        cm_after = store.load_content_map(root_path, "obj:new")
        # Digest for a.txt should be cleaned from content map
        # (unless it's still referenced by another logical path)
        inv_after = store.load_inventory(root_path, "obj:new")
        all_digests = set(inv_after.versions[inv_after.head].state.keys())
        if a_digest not in all_digests:
            assert a_digest not in cm_after

    def test_rm_nonexistent_path_errors(self, runner, storage_root, tmp_path):
        staging, _store, _root_path = _setup_staged(runner, storage_root, tmp_path)
        result = _invoke(runner, storage_root, staging, "rm", "obj:new", "nope.txt")
        assert result.exit_code == 1


# =========================================================================== #
# rm/mv pair `<path>.ots` companion in stage state when extension is active.
# Phase regression rules per `OTS workflow contract` transition matrix:
#   - drafting / content-pending  : stay
#   - content-upgraded            : regress to drafting (preserve content_proofs;
#                                   intersect pending_digests with new state)
#   - inventory-pending/-upgraded : regress to drafting + discard inventory_proof
#                                   + delete plan.json
#   - ots-cache/ entries          : never purged on regression
# =========================================================================== #


_SP_OK = """\
[simpleproof]
base_url = "https://stage.example"
proof_category_id = 641
access_token = "tok"
api_key = "key"
"""

_BTC_OK_COOKIE = """\
[bitcoin]
rpc_url = "http://127.0.0.1:8332"
zmq_endpoint = "tcp://127.0.0.1:28332"
rpc_user = "u"
rpc_password = "p"
confirmations = 3
"""


def _write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "ocfl-config" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(_SP_OK + _BTC_OK_COOKIE)
    monkeypatch.setenv("OCFL_CONFIG", str(cfg))
    return cfg


_CONTENT_A = b"alpha-content"
_CONTENT_B = b"beta-content"
_OTS_BYTES_A = b"\x00fake-ots-bytes-A-opaque"
_OTS_BYTES_B = b"\x00fake-ots-bytes-B-opaque"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _staging_dir(storage_root: Path, staging: Path, object_id: str) -> Path:
    return StagingStore(staging).staging_path(str(storage_root.resolve()), object_id)


def _add_file(
    runner: CliRunner,
    storage_root: Path,
    staging: Path,
    object_id: str,
    src_dir: Path,
    name: str,
    data: bytes,
) -> str:
    src = src_dir / name
    src.write_bytes(data)
    r = _invoke(runner, storage_root, staging, "add", object_id, str(src))
    assert r.exit_code == 0, r.output
    return _digest(data)


def _setup_ots_pair(
    runner: CliRunner,
    storage_root: Path,
    staging: Path,
    object_id: str,
    *,
    with_b: bool = False,
) -> tuple[Path, dict[str, str]]:
    """Create OTS staging with `a.txt` + `a.txt.ots` (optionally `b.txt` + `b.txt.ots`).

    Returns (staging_dir, digests) where digests maps logical path -> sha256.
    Phase is left at DRAFTING (the default after `stage new --ots`).
    """
    src_dir = staging.parent / f"src-{object_id.replace(':', '_')}"
    src_dir.mkdir(parents=True, exist_ok=True)

    r = _invoke(runner, storage_root, staging, "new", object_id, "--ots")
    assert r.exit_code == 0, r.output

    digests = {
        "a.txt": _add_file(
            runner, storage_root, staging, object_id, src_dir, "a.txt", _CONTENT_A
        ),
        "a.txt.ots": _add_file(
            runner,
            storage_root,
            staging,
            object_id,
            src_dir,
            "a.txt.ots",
            _OTS_BYTES_A,
        ),
    }
    if with_b:
        digests["b.txt"] = _add_file(
            runner, storage_root, staging, object_id, src_dir, "b.txt", _CONTENT_B
        )
        digests["b.txt.ots"] = _add_file(
            runner,
            storage_root,
            staging,
            object_id,
            src_dir,
            "b.txt.ots",
            _OTS_BYTES_B,
        )
    return _staging_dir(storage_root, staging, object_id), digests


def _state_paths(store: StagingStore, root_path: str, object_id: str) -> set[str]:
    inv = store.load_inventory(root_path, object_id)
    return {p for ps in inv.versions[inv.head].state.values() for p in ps}


def _state_digest_for(
    store: StagingStore, root_path: str, object_id: str, logical: str
) -> str | None:
    inv = store.load_inventory(root_path, object_id)
    for d, ps in inv.versions[inv.head].state.items():
        if logical in ps:
            return d
    return None


def _verified_proof() -> VerifiedProof:
    return VerifiedProof(
        ots_path="ots-cache/whatever.ots",
        verified=True,
        block_height=800_000,
        confirmations_at_verify=3,
        verified_at="2026-04-23T16:30:00+00:00",
    )


# --------------------------------------------------------------------------- #
# 1. rm pairs .ots removal at DRAFTING (extension active)
# --------------------------------------------------------------------------- #


class TestRmCompanionPairing:
    def test_rm_pairs_ots_removal_when_extension_active(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, _digests = _setup_ots_pair(runner, storage_root, staging, "obj:rm-pair")

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())

        # Pre: both files present at phase=drafting.
        before = _state_paths(store, root_path, "obj:rm-pair")
        assert {"a.txt", "a.txt.ots"} <= before
        assert load_ots(sd).phase is Phase.DRAFTING

        r = _invoke(runner, storage_root, staging, "rm", "obj:rm-pair", "a.txt")
        assert r.exit_code == 0, r.output

        after = _state_paths(store, root_path, "obj:rm-pair")
        assert "a.txt" not in after
        assert "a.txt.ots" not in after, (
            "rm must pair `.ots` companion when OTS extension is active"
        )
        # drafting + rm = stay (per transition matrix).
        assert load_ots(sd).phase is Phase.DRAFTING


# --------------------------------------------------------------------------- #
# 2. mv pairs .ots rename at DRAFTING (extension active)
# --------------------------------------------------------------------------- #


class TestMvCompanionPairing:
    def test_mv_pairs_ots_rename_when_extension_active(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, digests = _setup_ots_pair(runner, storage_root, staging, "obj:mv-pair")

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())

        r = _invoke(
            runner, storage_root, staging, "mv", "obj:mv-pair", "a.txt", "b.txt"
        )
        assert r.exit_code == 0, r.output

        after = _state_paths(store, root_path, "obj:mv-pair")
        assert "a.txt" not in after
        assert "a.txt.ots" not in after, (
            "mv must pair `.ots` companion when OTS extension is active"
        )
        assert "b.txt" in after
        assert "b.txt.ots" in after, (
            "mv must rename `.ots` companion alongside its data file"
        )

        # `.ots` rename keeps content → digest unchanged.
        assert (
            _state_digest_for(store, root_path, "obj:mv-pair", "b.txt")
            == digests["a.txt"]
        )
        assert (
            _state_digest_for(store, root_path, "obj:mv-pair", "b.txt.ots")
            == digests["a.txt.ots"]
        )

        # drafting + mv = stay.
        assert load_ots(sd).phase is Phase.DRAFTING


# --------------------------------------------------------------------------- #
# 3 & 4. Regression from CONTENT_UPGRADED preserves content_proofs
# --------------------------------------------------------------------------- #


def _pin_content_upgraded(sd: Path, da: str, db: str) -> None:
    """Mutate ots.json to phase=content-upgraded with verified proofs for da+db."""
    state = load_ots(sd)
    state = replace(
        state,
        phase=Phase.CONTENT_UPGRADED,
        pending_digests=(da, db),
        content_proofs={
            da: _verified_proof(),
            db: _verified_proof(),
        },
    )
    save_ots(sd, state)


class TestRegressionFromContentUpgraded:
    def test_rm_regresses_drafting_preserves_proofs_trims_pending_digests(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, digests = _setup_ots_pair(
            runner, storage_root, staging, "obj:rm-cu", with_b=True
        )
        da, db = digests["a.txt"], digests["b.txt"]
        _pin_content_upgraded(sd, da, db)

        r = _invoke(runner, storage_root, staging, "rm", "obj:rm-cu", "a.txt")
        assert r.exit_code == 0, r.output

        post = load_ots(sd)
        assert post.phase is Phase.DRAFTING, (
            "rm at content-upgraded must regress to drafting"
        )
        # content_proofs preserved across regression (per regression_rules).
        assert da in post.content_proofs
        assert db in post.content_proofs
        # pending_digests intersected with non_ots_digests of new state.
        assert set(post.pending_digests) == {db}, (
            f"pending_digests must be trimmed to digests still in state; "
            f"got {set(post.pending_digests)!r}"
        )

    def test_mv_regresses_to_drafting_preserves_content_proofs(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, digests = _setup_ots_pair(
            runner, storage_root, staging, "obj:mv-cu", with_b=True
        )
        da, db = digests["a.txt"], digests["b.txt"]
        _pin_content_upgraded(sd, da, db)

        r = _invoke(runner, storage_root, staging, "mv", "obj:mv-cu", "a.txt", "c.txt")
        assert r.exit_code == 0, r.output

        post = load_ots(sd)
        assert post.phase is Phase.DRAFTING
        # mv keeps digests unchanged → both proofs and pending_digests carry.
        assert da in post.content_proofs
        assert db in post.content_proofs
        assert set(post.pending_digests) == {da, db}, (
            "mv keeps digests; pending_digests should still contain da and db"
        )


# --------------------------------------------------------------------------- #
# 5 & 6. Regression from inventory-* discards inventory_proof + plan.json
# --------------------------------------------------------------------------- #


def _pin_inventory_state(sd: Path, da: str, db: str, *, phase: Phase) -> None:
    """Mutate ots.json to *phase* (inventory-pending or -upgraded) with an
    inventory_proof set and content_proofs/pending_digests populated."""
    state = load_ots(sd)
    state = replace(
        state,
        phase=phase,
        pending_digests=(da, db),
        content_proofs={
            da: _verified_proof(),
            db: _verified_proof(),
        },
        inventory_proof=InventoryProof(
            version="v1",
            digest="deadbeef" * 8,
            job_id="job-xyz",
            proof=_verified_proof(),
        ),
    )
    save_ots(sd, state)


class TestRegressionFromInventoryPending:
    def test_rm_discards_inventory_proof_and_deletes_plan_json(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, digests = _setup_ots_pair(
            runner, storage_root, staging, "obj:rm-ip", with_b=True
        )
        da, db = digests["a.txt"], digests["b.txt"]
        _pin_inventory_state(sd, da, db, phase=Phase.INVENTORY_PENDING)

        # Sentinel plan.json — content irrelevant, only existence matters here.
        plan_path(sd).write_bytes(b"sentinel-plan-json")
        assert plan_path(sd).is_file()

        r = _invoke(runner, storage_root, staging, "rm", "obj:rm-ip", "a.txt")
        assert r.exit_code == 0, r.output

        post = load_ots(sd)
        assert post.phase is Phase.DRAFTING
        assert post.inventory_proof is None, (
            "regression from inventory-* must discard inventory_proof"
        )
        assert not plan_path(sd).exists(), (
            "regression from inventory-* must delete plan.json"
        )
        # content_proofs preserved (carry-forward).
        assert da in post.content_proofs
        assert db in post.content_proofs
        # pending_digests trimmed to digests still in state (a.txt removed).
        assert set(post.pending_digests) == {db}


class TestRegressionFromInventoryUpgraded:
    def test_mv_discards_inventory_proof_and_deletes_plan_json(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, digests = _setup_ots_pair(
            runner, storage_root, staging, "obj:mv-iu", with_b=True
        )
        da, db = digests["a.txt"], digests["b.txt"]
        _pin_inventory_state(sd, da, db, phase=Phase.INVENTORY_UPGRADED)

        plan_path(sd).write_bytes(b"sentinel-plan-json")
        assert plan_path(sd).is_file()

        r = _invoke(runner, storage_root, staging, "mv", "obj:mv-iu", "a.txt", "c.txt")
        assert r.exit_code == 0, r.output

        post = load_ots(sd)
        assert post.phase is Phase.DRAFTING
        assert post.inventory_proof is None
        assert not plan_path(sd).exists()
        # content_proofs preserved across regression.
        assert da in post.content_proofs
        assert db in post.content_proofs


# --------------------------------------------------------------------------- #
# 7. ots-cache/ entries are NOT purged on regression
# --------------------------------------------------------------------------- #


class TestOtsCacheNotPurgedOnRegression:
    def test_rm_does_not_purge_ots_cache(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, digests = _setup_ots_pair(
            runner, storage_root, staging, "obj:rm-cache", with_b=True
        )
        da, db = digests["a.txt"], digests["b.txt"]
        _pin_inventory_state(sd, da, db, phase=Phase.INVENTORY_UPGRADED)

        cache = ots_cache_path(sd, da)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(b"cached-ots-bytes")
        assert cache.is_file()

        r = _invoke(runner, storage_root, staging, "rm", "obj:rm-cache", "a.txt")
        assert r.exit_code == 0, r.output

        assert load_ots(sd).phase is Phase.DRAFTING
        assert cache.is_file(), (
            "ots-cache/ entries must NOT be purged on regression "
            "(content-addressed by digest; only purged on commit/reset)"
        )


# --------------------------------------------------------------------------- #
# 8 & 9. Non-OTS rm/mv unchanged
# --------------------------------------------------------------------------- #


class TestNonOtsRegression:
    def test_rm_without_ots_extension_unchanged(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
    ) -> None:
        staging, store, root_path = _setup_staged(runner, storage_root, tmp_path)
        sd = _staging_dir(storage_root, staging, "obj:new")
        assert not has_ots(sd), "non-OTS staging must not have ots.json"

        r = _invoke(runner, storage_root, staging, "rm", "obj:new", "a.txt")
        assert r.exit_code == 0, r.output

        paths = _state_paths(store, root_path, "obj:new")
        assert "a.txt" not in paths
        assert not has_ots(sd), "rm must not create ots.json on non-OTS staging"
        # No phase-related noise on stderr.
        assert "phase" not in (r.output or "").lower()

    def test_mv_without_ots_extension_unchanged(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
    ) -> None:
        staging, store, root_path = _setup_staged(runner, storage_root, tmp_path)
        sd = _staging_dir(storage_root, staging, "obj:new")
        assert not has_ots(sd)

        r = _invoke(
            runner, storage_root, staging, "mv", "obj:new", "a.txt", "renamed.txt"
        )
        assert r.exit_code == 0, r.output

        paths = _state_paths(store, root_path, "obj:new")
        assert "a.txt" not in paths
        assert "renamed.txt" in paths
        assert not has_ots(sd)
        assert "phase" not in (r.output or "").lower()
