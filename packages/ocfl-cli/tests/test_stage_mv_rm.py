import hashlib
from pathlib import Path
from typing import NamedTuple

import pytest
from msgspec.structs import replace
from ots_helpers import write_config
from runner_helpers import staging_dir
from typer.testing import CliRunner

from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app
from ocfl_cli.model.ots_state import Proof, StagedInventory
from ocfl_cli.model.phase import Phase, TimestampTarget
from ocfl_cli.store.ots_state import (
    has_ots,
    load_ots,
    ots_cache_path,
    plan_path,
    save_ots,
)
from ocfl_cli.store.staging import StagingStore


@pytest.fixture
def runner():
    return CliRunner()


def _invoke(runner, storage_root, staging, *args):
    return runner.invoke(
        app,
        ["--root", str(storage_root), "stage", "--staging-root", str(staging), *args],
    )


def _setup_staged(runner, storage_root, tmp_path):
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
        assert result.exit_code == ExitCode.SUCCESS, result.output

        inv = store.load_inventory(root_path, "obj:new")
        paths = [p for ps in inv.versions["v1"].state.values() for p in ps]
        assert "renamed.txt" in paths
        assert "a.txt" not in paths

    def test_mv_nonexistent_path_errors(self, runner, storage_root, tmp_path):
        staging, _store, _root_path = _setup_staged(runner, storage_root, tmp_path)
        result = _invoke(
            runner, storage_root, staging, "mv", "obj:new", "nope.txt", "dst.txt"
        )
        assert result.exit_code == ExitCode.GENERIC

    def test_mv_to_existing_path_errors_without_duplicate_state(
        self, runner, storage_root, tmp_path
    ):
        staging, store, root_path = _setup_staged(runner, storage_root, tmp_path)
        result = _invoke(
            runner, storage_root, staging, "mv", "obj:new", "a.txt", "dir/b.txt"
        )
        assert result.exit_code == ExitCode.GENERIC
        assert "already exists" in result.output

        inv = store.load_inventory(root_path, "obj:new")
        paths = [p for ps in inv.versions["v1"].state.values() for p in ps]
        assert paths.count("dir/b.txt") == 1
        assert "a.txt" in paths

    def test_mv_to_existing_companion_path_errors_without_duplicate_state(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        _sd, _digests = _new_drafting_ots_staging(
            runner, storage_root, staging, "obj:mv-ots"
        )

        src = tmp_path / "b.txt.ots"
        src.write_bytes(b"existing companion")
        r = _invoke(runner, storage_root, staging, "add", "obj:mv-ots", str(src))
        assert r.exit_code == ExitCode.SUCCESS, r.output

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        before = _state_paths(store, root_path, "obj:mv-ots")
        assert {"a.txt", "a.txt.ots", "b.txt.ots"} <= before

        r = _invoke(runner, storage_root, staging, "mv", "obj:mv-ots", "a.txt", "b.txt")
        assert r.exit_code == ExitCode.GENERIC
        assert "already exists" in r.output

        after = _state_paths(store, root_path, "obj:mv-ots")
        assert after == before


class TestStageRm:
    def test_rm_exact_path(self, runner, storage_root, tmp_path):
        staging, store, root_path = _setup_staged(runner, storage_root, tmp_path)
        result = _invoke(runner, storage_root, staging, "rm", "obj:new", "a.txt")
        assert result.exit_code == ExitCode.SUCCESS, result.output

        inv = store.load_inventory(root_path, "obj:new")
        paths = [p for ps in inv.versions["v1"].state.values() for p in ps]
        assert "a.txt" not in paths
        assert "dir/b.txt" in paths

    def test_rm_recursive(self, runner, storage_root, tmp_path):
        staging, store, root_path = _setup_staged(runner, storage_root, tmp_path)
        result = _invoke(
            runner, storage_root, staging, "rm", "obj:new", "dir", "--recursive"
        )
        assert result.exit_code == ExitCode.SUCCESS, result.output

        inv = store.load_inventory(root_path, "obj:new")
        paths = [p for ps in inv.versions["v1"].state.values() for p in ps]
        assert "dir/b.txt" not in paths
        assert "dir/c.txt" not in paths
        assert "a.txt" in paths

    def test_rm_cleans_unreferenced_content(self, runner, storage_root, tmp_path):
        staging, store, root_path = _setup_staged(runner, storage_root, tmp_path)
        inv = store.load_inventory(root_path, "obj:new")
        [a_digest] = [d for d, ps in inv.versions["v1"].state.items() if "a.txt" in ps]
        assert a_digest in store.load_content_map(root_path, "obj:new")

        _invoke(runner, storage_root, staging, "rm", "obj:new", "a.txt")

        assert a_digest not in store.load_content_map(root_path, "obj:new")

    def test_rm_nonexistent_path_errors(self, runner, storage_root, tmp_path):
        staging, _store, _root_path = _setup_staged(runner, storage_root, tmp_path)
        result = _invoke(runner, storage_root, staging, "rm", "obj:new", "nope.txt")
        assert result.exit_code == ExitCode.GENERIC


_CONTENT_A = b"alpha-content"
_CONTENT_B = b"beta-content"
_OTS_BYTES_A = b"\x00fake-ots-bytes-A-opaque"
_OTS_BYTES_B = b"\x00fake-ots-bytes-B-opaque"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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
    assert r.exit_code == ExitCode.SUCCESS, r.output
    return _digest(data)


class DraftingOtsStaging(NamedTuple):
    staging_dir: Path
    sha256_by_path: dict[str, str]


def _new_drafting_ots_staging(
    runner: CliRunner,
    storage_root: Path,
    staging: Path,
    object_id: str,
    *,
    with_b: bool = False,
) -> DraftingOtsStaging:
    src_dir = staging.parent / f"src-{object_id.replace(':', '_')}"
    src_dir.mkdir(parents=True, exist_ok=True)

    r = _invoke(runner, storage_root, staging, "new", object_id, "--ots")
    assert r.exit_code == ExitCode.SUCCESS, r.output

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
    return DraftingOtsStaging(staging_dir(storage_root, staging, object_id), digests)


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


def _verified_proof() -> Proof:
    return Proof(
        target=TimestampTarget.CONTENT,
        ots_path="ots-cache/whatever.ots",
        verified=True,
        block_height=800_000,
        confirmations_at_verify=3,
        verified_at="2026-04-23T16:30:00+00:00",
    )


class TestRmCompanionPairing:
    def test_rm_pairs_ots_removal_when_extension_active(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, _digests = _new_drafting_ots_staging(
            runner, storage_root, staging, "obj:rm-pair"
        )

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())

        before = _state_paths(store, root_path, "obj:rm-pair")
        assert {"a.txt", "a.txt.ots"} <= before
        assert load_ots(sd).phase is Phase.DRAFTING

        r = _invoke(runner, storage_root, staging, "rm", "obj:rm-pair", "a.txt")
        assert r.exit_code == ExitCode.SUCCESS, r.output

        after = _state_paths(store, root_path, "obj:rm-pair")
        assert "a.txt" not in after
        assert "a.txt.ots" not in after, (
            "rm must pair `.ots` companion when OTS extension is active"
        )
        assert load_ots(sd).phase is Phase.DRAFTING


class TestMvCompanionPairing:
    def test_mv_pairs_ots_rename_when_extension_active(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, digests = _new_drafting_ots_staging(
            runner, storage_root, staging, "obj:mv-pair"
        )

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())

        r = _invoke(
            runner, storage_root, staging, "mv", "obj:mv-pair", "a.txt", "b.txt"
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output

        after = _state_paths(store, root_path, "obj:mv-pair")
        assert "a.txt" not in after
        assert "a.txt.ots" not in after, (
            "mv must pair `.ots` companion when OTS extension is active"
        )
        assert "b.txt" in after
        assert "b.txt.ots" in after, (
            "mv must rename `.ots` companion alongside its data file"
        )

        assert (
            _state_digest_for(store, root_path, "obj:mv-pair", "b.txt")
            == digests["a.txt"]
        )
        assert (
            _state_digest_for(store, root_path, "obj:mv-pair", "b.txt.ots")
            == digests["a.txt.ots"]
        )

        assert load_ots(sd).phase is Phase.DRAFTING


def _pin_content_upgraded(sd: Path, da: str, db: str) -> None:
    state = load_ots(sd)
    state = replace(
        state,
        phase=Phase.CONTENT_UPGRADED,
        pending_digests=(da, db),
        proofs={
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
        write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, digests = _new_drafting_ots_staging(
            runner, storage_root, staging, "obj:rm-cu", with_b=True
        )
        da, db = digests["a.txt"], digests["b.txt"]
        _pin_content_upgraded(sd, da, db)

        r = _invoke(runner, storage_root, staging, "rm", "obj:rm-cu", "a.txt")
        assert r.exit_code == ExitCode.SUCCESS, r.output

        post = load_ots(sd)
        assert post.phase is Phase.DRAFTING, (
            "rm at content-upgraded must regress to drafting"
        )
        assert da in post.proofs
        assert db in post.proofs
        assert set(post.pending_digests) == {db}, (
            f"pending_digests must be trimmed to digests still in state; "
            f"got {set(post.pending_digests)!r}"
        )

    def test_mv_regresses_to_drafting_preserves_proofs(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, digests = _new_drafting_ots_staging(
            runner, storage_root, staging, "obj:mv-cu", with_b=True
        )
        da, db = digests["a.txt"], digests["b.txt"]
        _pin_content_upgraded(sd, da, db)

        r = _invoke(runner, storage_root, staging, "mv", "obj:mv-cu", "a.txt", "c.txt")
        assert r.exit_code == ExitCode.SUCCESS, r.output

        post = load_ots(sd)
        assert post.phase is Phase.DRAFTING
        assert da in post.proofs
        assert db in post.proofs
        assert set(post.pending_digests) == {da, db}, (
            "mv keeps digests; pending_digests should still contain da and db"
        )

    def test_add_regresses_to_drafting_preserves_proofs(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, digests = _new_drafting_ots_staging(
            runner, storage_root, staging, "obj:add-cu", with_b=True
        )
        da, db = digests["a.txt"], digests["b.txt"]
        _pin_content_upgraded(sd, da, db)

        src = tmp_path / "new-content.txt"
        src.write_text("new content")
        r = _invoke(runner, storage_root, staging, "add", "obj:add-cu", str(src))
        assert r.exit_code == ExitCode.SUCCESS, r.output

        post = load_ots(sd)
        assert post.phase is Phase.DRAFTING
        assert da in post.proofs
        assert db in post.proofs
        assert set(post.pending_digests) == {da, db}


INV_DIGEST = "deadbeef" * 8


def _pin_inventory_state(sd: Path, da: str, db: str, *, phase: Phase) -> None:
    state = load_ots(sd)
    state = replace(
        state,
        phase=phase,
        pending_digests=(da, db),
        proofs={
            da: _verified_proof(),
            db: _verified_proof(),
            INV_DIGEST: replace(_verified_proof(), target=TimestampTarget.INVENTORY),
        },
        inventory=StagedInventory(version="v1", digest=INV_DIGEST, job_id="job-xyz"),
    )
    save_ots(sd, state)


class TestRegressionFromInventoryPending:
    def test_rm_drops_staged_inventory_and_keeps_plan_json(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, digests = _new_drafting_ots_staging(
            runner, storage_root, staging, "obj:rm-ip", with_b=True
        )
        da, db = digests["a.txt"], digests["b.txt"]
        _pin_inventory_state(sd, da, db, phase=Phase.INVENTORY_PENDING)

        plan_path(sd).write_bytes(b"sentinel-plan-json")
        assert plan_path(sd).is_file()

        r = _invoke(runner, storage_root, staging, "rm", "obj:rm-ip", "a.txt")
        assert r.exit_code == ExitCode.SUCCESS, r.output

        post = load_ots(sd)
        assert post.phase is Phase.DRAFTING
        assert post.inventory is None, (
            "regression from inventory-* must drop the staged inventory"
        )
        assert INV_DIGEST in post.proofs, "the inventory proof stays (ADR-0004)"
        assert plan_path(sd).exists(), "plan.json stays for a byte-identical rebuild"

        assert da in post.proofs
        assert db in post.proofs
        assert set(post.pending_digests) == {db}

    def test_add_drops_staged_inventory_and_keeps_plan_json(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, digests = _new_drafting_ots_staging(
            runner, storage_root, staging, "obj:add-ip", with_b=True
        )
        da, db = digests["a.txt"], digests["b.txt"]
        _pin_inventory_state(sd, da, db, phase=Phase.INVENTORY_PENDING)

        plan_path(sd).write_bytes(b"sentinel-plan-json")
        assert plan_path(sd).is_file()

        src = tmp_path / "new-content.txt"
        src.write_text("new content")
        r = _invoke(runner, storage_root, staging, "add", "obj:add-ip", str(src))
        assert r.exit_code == ExitCode.SUCCESS, r.output

        post = load_ots(sd)
        assert post.phase is Phase.DRAFTING
        assert post.inventory is None
        assert INV_DIGEST in post.proofs
        assert plan_path(sd).exists()
        assert da in post.proofs
        assert db in post.proofs


class TestRegressionFromInventoryUpgraded:
    def test_mv_drops_staged_inventory_and_keeps_plan_json(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, digests = _new_drafting_ots_staging(
            runner, storage_root, staging, "obj:mv-iu", with_b=True
        )
        da, db = digests["a.txt"], digests["b.txt"]
        _pin_inventory_state(sd, da, db, phase=Phase.INVENTORY_UPGRADED)

        plan_path(sd).write_bytes(b"sentinel-plan-json")
        assert plan_path(sd).is_file()

        r = _invoke(runner, storage_root, staging, "mv", "obj:mv-iu", "a.txt", "c.txt")
        assert r.exit_code == ExitCode.SUCCESS, r.output

        post = load_ots(sd)
        assert post.phase is Phase.DRAFTING
        assert post.inventory is None
        assert INV_DIGEST in post.proofs
        assert plan_path(sd).exists()
        assert da in post.proofs
        assert db in post.proofs

    def test_add_drops_staged_inventory_and_keeps_plan_json(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, digests = _new_drafting_ots_staging(
            runner, storage_root, staging, "obj:add-iu", with_b=True
        )
        da, db = digests["a.txt"], digests["b.txt"]
        _pin_inventory_state(sd, da, db, phase=Phase.INVENTORY_UPGRADED)

        plan_path(sd).write_bytes(b"sentinel-plan-json")
        assert plan_path(sd).is_file()

        src = tmp_path / "new-content.txt"
        src.write_text("new content")
        r = _invoke(runner, storage_root, staging, "add", "obj:add-iu", str(src))
        assert r.exit_code == ExitCode.SUCCESS, r.output

        post = load_ots(sd)
        assert post.phase is Phase.DRAFTING
        assert post.inventory is None
        assert INV_DIGEST in post.proofs
        assert plan_path(sd).exists()
        assert da in post.proofs
        assert db in post.proofs


class TestOtsCacheNotPurgedOnRegression:
    def test_rm_does_not_purge_ots_cache(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_config(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        sd, digests = _new_drafting_ots_staging(
            runner, storage_root, staging, "obj:rm-cache", with_b=True
        )
        da, db = digests["a.txt"], digests["b.txt"]
        _pin_inventory_state(sd, da, db, phase=Phase.INVENTORY_UPGRADED)

        cache = ots_cache_path(sd, da)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(b"cached-ots-bytes")
        assert cache.is_file()

        r = _invoke(runner, storage_root, staging, "rm", "obj:rm-cache", "a.txt")
        assert r.exit_code == ExitCode.SUCCESS, r.output

        assert load_ots(sd).phase is Phase.DRAFTING
        assert cache.is_file(), (
            "ots-cache/ entries must NOT be purged on regression "
            "(content-addressed by digest; only purged on commit/discard)"
        )


class TestNonOtsRegression:
    def test_rm_without_ots_extension_unchanged(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
    ) -> None:
        staging, store, root_path = _setup_staged(runner, storage_root, tmp_path)
        sd = staging_dir(storage_root, staging, "obj:new")
        assert not has_ots(sd), "non-OTS staging must not have ots.json"

        r = _invoke(runner, storage_root, staging, "rm", "obj:new", "a.txt")
        assert r.exit_code == ExitCode.SUCCESS, r.output

        paths = _state_paths(store, root_path, "obj:new")
        assert "a.txt" not in paths
        assert not has_ots(sd), "rm must not create ots.json on non-OTS staging"
        assert "phase" not in (r.output or "").lower()

    def test_mv_without_ots_extension_unchanged(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
    ) -> None:
        staging, store, root_path = _setup_staged(runner, storage_root, tmp_path)
        sd = staging_dir(storage_root, staging, "obj:new")
        assert not has_ots(sd)

        r = _invoke(
            runner, storage_root, staging, "mv", "obj:new", "a.txt", "renamed.txt"
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output

        paths = _state_paths(store, root_path, "obj:new")
        assert "a.txt" not in paths
        assert "renamed.txt" in paths
        assert not has_ots(sd)
        assert "phase" not in (r.output or "").lower()
