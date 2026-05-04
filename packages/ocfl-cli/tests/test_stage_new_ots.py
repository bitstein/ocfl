"""Tests for `ocfl stage new --ots` (OTS workflow step 9).

Pins step 9 of `OTS workflow contract`:
- `--ots` flag forces sha256 (rejects any other `--digest` with explicit
  SHA-256 reference; non-zero exit).
- `--ots` requires `[simpleproof]` + `[bitcoin]` config to be present and
  valid; missing sections fail early with actionable hints (config error
  → exit 6 per the canonical exit-code mapping).
- On success, writes initial `<staging>/ots.json` at schema_version=1
  with phase=drafting, category_id=[simpleproof].proof_category_id,
  empty submissions/verify_jobs/content_proofs/pending_digests, and
  inventory_proof=None. Staging inventory uses sha256 digest algorithm.
- Extension config (`extensions/NNNN-ots-timestamps/config.json`) is NOT
  written here — that lands at first commit (step 9 deliverable bullet).
- Regression: plain `stage new` (no `--ots`) writes no `ots.json` and
  preserves pre-OTS behavior.
- Re-running `stage new --ots` for the same (root, object) is rejected
  (existing-staging guard inherited from `stage new`).
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ocfl_cli.main import app
from ocfl_cli.stage.fsm import Phase
from ocfl_cli.stage.state import has_ots, load_ots
from ocfl_cli.stage.store import StagingStore

# --------------------------------------------------------------------------- #
# Config fixtures
# --------------------------------------------------------------------------- #

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
"""


def _write_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: str,
) -> Path:
    """Write *body* to a config.toml under *tmp_path* and point OCFL_CONFIG at it."""
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


# --------------------------------------------------------------------------- #
# Layer-1: digest forcing
# --------------------------------------------------------------------------- #


class TestDigestForcing:
    def test_ots_with_sha512_rejected(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`--ots --digest sha512` must reject with explicit SHA-256 message."""
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
        # No staging dir should have been created.
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
        """`--ots --digest sha256` is the no-op redundant case; must succeed."""
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


# --------------------------------------------------------------------------- #
# Layer-1: config presence (fail early per resolved question 4)
# --------------------------------------------------------------------------- #


class TestConfigValidation:
    def test_missing_simpleproof_rejected(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`--ots` without `[simpleproof]` must reject with actionable hint."""
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
        # No staging dir should have been created.
        store = StagingStore(staging)
        assert not store.exists(str(storage_root.resolve()), "obj:new")

    def test_missing_bitcoin_rejected(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`--ots` without `[bitcoin]` must reject with actionable hint."""
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
        """Empty config: `--ots` rejects, hint mentions at least one section."""
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


# --------------------------------------------------------------------------- #
# Layer-2: initial ots.json shape
# --------------------------------------------------------------------------- #


class TestInitialOtsJson:
    def test_ots_json_initialized(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`--ots` writes initial ots.json with the schema-v1 shape per
        `staging_ots_json.schema` and `commands.stage_new_ots.behavior`."""
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

        assert state.schema_version == 1
        assert state.phase is Phase.DRAFTING
        # Matches [simpleproof].proof_category_id from _SP_OK.
        assert state.category_id == 641
        assert state.pending_digests == ()
        assert state.submissions == ()
        assert state.verify_jobs == ()
        assert state.content_proofs == {}
        assert state.inventory_proof is None

    def test_staging_inventory_uses_sha256(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`--ots` forces sha256 even when no `--digest` given."""
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
        """`category_id` in fresh `ots.json` reflects current
        `[simpleproof].proof_category_id`."""
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
        """Extension config lands at first commit, not `stage new`. Step 9
        deliverable bullet 5 explicitly carves this out."""
        _write_config(tmp_path, monkeypatch, _SP_OK + _BTC_OK_COOKIE)
        staging = tmp_path / "staging"
        result = runner.invoke(
            app,
            _stage_new_ots_args(storage_root, staging, "obj:new"),
        )
        assert result.exit_code == 0, result.output

        # No `extensions/` directory should appear in the storage root yet.
        ext_dir = storage_root / "extensions"
        assert not ext_dir.exists()


# --------------------------------------------------------------------------- #
# Layer-2: rejection of double-OTS staging (existing-exists guard)
# --------------------------------------------------------------------------- #


class TestDoubleOtsStaging:
    def test_second_ots_invocation_rejected(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A second `stage new --ots` for the same (root, object) must fail —
        same guard as plain `stage new`."""
        _write_config(tmp_path, monkeypatch, _SP_OK + _BTC_OK_COOKIE)
        staging = tmp_path / "staging"
        args = _stage_new_ots_args(storage_root, staging, "obj:new")

        first = runner.invoke(app, args)
        assert first.exit_code == 0, first.output

        second = runner.invoke(app, args)
        assert second.exit_code != 0, second.output
        combined = (second.output + (second.stderr or "")).lower()
        assert "already" in combined


# --------------------------------------------------------------------------- #
# Layer-2: regression — plain `stage new` (no `--ots`) preserved
# --------------------------------------------------------------------------- #


class TestNonOtsRegression:
    def test_no_ots_json_without_flag(
        self,
        runner: CliRunner,
        storage_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Plain `stage new` writes no `ots.json` even when the OTS config
        sections are present in user config."""
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
