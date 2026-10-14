from pathlib import Path

import pytest
from typer.testing import CliRunner

from ocfl_cli.main import app
from ocfl_cli.model.phase import Phase
from ocfl_cli.store.ots_state import has_ots, load_ots
from ocfl_cli.store.staging import StagingStore

_SP_OK = """\
[simpleproof]
base_url = "https://stage.example"
proof_category_id = 641
"""

_BTC_OK_COOKIE = """\
[bitcoin]
rpc_url = "http://127.0.0.1:8332"
zmq_endpoint = "tcp://127.0.0.1:28332"
"""


def _write_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: str,
) -> Path:
    cfg = tmp_path / "ocfl-config" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(body)
    monkeypatch.setenv("OCFL_CONFIG", str(cfg))
    return cfg


def _stage_new_ots_args(
    storage_root: Path,
    staging: Path,
    object_id: str,
    *extra: str,
) -> list[str]:
    return [
        "--root",
        str(storage_root),
        "stage",
        "--staging-root",
        str(staging),
        "new",
        object_id,
        "--ots",
        *extra,
    ]


def _strip_paths(text: str) -> str:
    """Return *text* with any tmp_path-flavored path tokens removed.

    Test parametrize ids (e.g. ``sha512``) and section names (``simpleproof``,
    ``bitcoin``) leak into pytest-managed temp directory paths. Substring
    matching against raw output therefore falsely matches paths that the
    OS happens to print in error messages. Strip whitespace-delimited tokens
    that look like absolute paths before keyword-matching.
    """
    return " ".join(tok for tok in text.split() if not tok.startswith("/"))


class TestDigestForcing:
    def test_ots_with_sha512_rejected(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch, _SP_OK + _BTC_OK_COOKIE)
        staging = tmp_path / "staging"
        result = runner.invoke(
            app,
            _stage_new_ots_args(storage_root, staging, "obj:new", "--digest", "sha512"),
        )
        assert result.exit_code != 0, result.output
        combined = _strip_paths((result.output + (result.stderr or "")).lower())
        # Rejection comes from our code, not Typer's unknown-option handler.
        assert "no such option" not in combined, result.output
        assert "sha-256" in combined or "sha256" in combined, result.output
        store = StagingStore(staging)
        assert not store.exists(str(storage_root.resolve()), "obj:new")

    @pytest.mark.parametrize("alg", ["sha512", "blake2b-512"])
    def test_ots_with_non_sha256_rejected(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        alg: str,
    ) -> None:
        _write_config(tmp_path, monkeypatch, _SP_OK + _BTC_OK_COOKIE)
        staging = tmp_path / "staging"
        result = runner.invoke(
            app,
            _stage_new_ots_args(storage_root, staging, "obj:new", "--digest", alg),
        )
        assert result.exit_code != 0, result.output
        combined = _strip_paths((result.output + (result.stderr or "")).lower())
        assert "no such option" not in combined, result.output
        assert "sha-256" in combined or "sha256" in combined, result.output
        store = StagingStore(staging)
        assert not store.exists(str(storage_root.resolve()), "obj:new")

    def test_ots_with_sha256_explicit_ok(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch, _SP_OK + _BTC_OK_COOKIE)
        staging = tmp_path / "staging"
        result = runner.invoke(
            app,
            _stage_new_ots_args(storage_root, staging, "obj:new", "--digest", "sha256"),
        )
        assert result.exit_code == 0, result.output
        store = StagingStore(staging)
        inv = store.load_inventory(str(storage_root.resolve()), "obj:new")
        assert inv.digest_algorithm == "sha256"

    def test_ots_existing_sha512_object_rejected(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch, _SP_OK + _BTC_OK_COOKIE)
        staging = tmp_path / "staging"
        result = runner.invoke(
            app,
            _stage_new_ots_args(storage_root, staging, "obj:test"),
        )

        assert result.exit_code != 0, result.output
        combined = _strip_paths((result.output + (result.stderr or "")).lower())
        assert "sha-256" in combined or "sha256" in combined, result.output
        store = StagingStore(staging)
        assert not store.exists(str(storage_root.resolve()), "obj:test")


class TestConfigValidation:
    def test_missing_simpleproof_rejected(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch, _BTC_OK_COOKIE)  # only [bitcoin]
        staging = tmp_path / "staging"
        result = runner.invoke(
            app,
            _stage_new_ots_args(storage_root, staging, "obj:new"),
        )
        assert result.exit_code == 6, result.output
        combined = _strip_paths((result.output + (result.stderr or "")).lower())
        assert "no such option" not in combined, result.output
        assert "simpleproof" in combined, result.output
        store = StagingStore(staging)
        assert not store.exists(str(storage_root.resolve()), "obj:new")

    def test_missing_bitcoin_rejected(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch, _SP_OK)  # only [simpleproof]
        staging = tmp_path / "staging"
        result = runner.invoke(
            app,
            _stage_new_ots_args(storage_root, staging, "obj:new"),
        )
        assert result.exit_code == 6, result.output
        combined = _strip_paths((result.output + (result.stderr or "")).lower())
        assert "no such option" not in combined, result.output
        assert "bitcoin" in combined, result.output
        store = StagingStore(staging)
        assert not store.exists(str(storage_root.resolve()), "obj:new")

    def test_both_sections_missing_rejected(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch, "")
        staging = tmp_path / "staging"
        result = runner.invoke(
            app,
            _stage_new_ots_args(storage_root, staging, "obj:new"),
        )
        assert result.exit_code == 6, result.output
        combined = _strip_paths((result.output + (result.stderr or "")).lower())
        assert "no such option" not in combined, result.output
        assert "simpleproof" in combined or "bitcoin" in combined, result.output
        store = StagingStore(staging)
        assert not store.exists(str(storage_root.resolve()), "obj:new")


class TestInitialOtsJson:
    def test_ots_json_initialized(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch, _SP_OK + _BTC_OK_COOKIE)
        staging = tmp_path / "staging"
        result = runner.invoke(
            app,
            _stage_new_ots_args(storage_root, staging, "obj:new"),
        )
        assert result.exit_code == 0, result.output

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        assert store.exists(root_path, "obj:new")
        staging_dir = store.staging_path(root_path, "obj:new")

        assert has_ots(staging_dir)
        state = load_ots(staging_dir)

        assert state.schema_version == 2
        assert state.phase is Phase.DRAFTING
        # Matches [simpleproof].proof_category_id from _SP_OK.
        assert state.category_id == 641
        assert state.pending_digests == ()
        assert state.submissions == ()
        assert state.verify_jobs == ()
        assert state.proofs == {}
        assert state.inventory_proof is None

    def test_staging_inventory_uses_sha256(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch, _SP_OK + _BTC_OK_COOKIE)
        staging = tmp_path / "staging"
        result = runner.invoke(
            app,
            _stage_new_ots_args(storage_root, staging, "obj:new"),
        )
        assert result.exit_code == 0, result.output

        store = StagingStore(staging)
        inv = store.load_inventory(str(storage_root.resolve()), "obj:new")
        assert inv.digest_algorithm == "sha256"

    def test_category_id_picked_up_from_config(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        custom = _SP_OK.replace("proof_category_id = 641", "proof_category_id = 7777")
        _write_config(tmp_path, monkeypatch, custom + _BTC_OK_COOKIE)
        staging = tmp_path / "staging"
        result = runner.invoke(
            app,
            _stage_new_ots_args(storage_root, staging, "obj:new"),
        )
        assert result.exit_code == 0, result.output

        store = StagingStore(staging)
        staging_dir = store.staging_path(str(storage_root.resolve()), "obj:new")
        state = load_ots(staging_dir)
        assert state.category_id == 7777

    def test_extension_config_not_written_at_stage_new(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch, _SP_OK + _BTC_OK_COOKIE)
        staging = tmp_path / "staging"
        result = runner.invoke(
            app,
            _stage_new_ots_args(storage_root, staging, "obj:new"),
        )
        assert result.exit_code == 0, result.output

        ext_dir = storage_root / "extensions"
        assert not ext_dir.exists()


class TestDoubleOtsStaging:
    def test_second_ots_invocation_rejected(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch, _SP_OK + _BTC_OK_COOKIE)
        staging = tmp_path / "staging"
        args = _stage_new_ots_args(storage_root, staging, "obj:new")

        first = runner.invoke(app, args)
        assert first.exit_code == 0, first.output

        second = runner.invoke(app, args)
        assert second.exit_code != 0, second.output
        combined = (second.output + (second.stderr or "")).lower()
        assert "already" in combined


class TestNonOtsRegression:
    def test_no_ots_json_without_flag(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch, _SP_OK + _BTC_OK_COOKIE)
        staging = tmp_path / "staging"
        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "stage",
                "--staging-root",
                str(staging),
                "new",
                "obj:new",
            ],
        )
        assert result.exit_code == 0, result.output

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        assert store.exists(root_path, "obj:new")
        staging_dir = store.staging_path(root_path, "obj:new")
        assert not has_ots(staging_dir)

    def test_no_ots_no_config_required(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
    ) -> None:
        """Plain `stage new` succeeds with no OTS config at all — autouse
        `_isolate_user_config` already points OCFL_CONFIG at a nonexistent
        sentinel, so this is the default."""
        staging = tmp_path / "staging"
        result = runner.invoke(
            app,
            [
                "--root",
                str(storage_root),
                "stage",
                "--staging-root",
                str(staging),
                "new",
                "obj:new",
            ],
        )
        assert result.exit_code == 0, result.output

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        staging_dir = store.staging_path(root_path, "obj:new")
        assert not has_ots(staging_dir)
