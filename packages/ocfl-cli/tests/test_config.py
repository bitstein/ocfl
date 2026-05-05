"""Tests for ocfl CLI user config (~/.config/ocfl/config.toml)."""

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ocfl_cli.config import (
    ArchiveConfig,
    Config,
    ConfigError,
    config_path,
    load_config,
)
from ocfl_cli.main import app
from ocfl_lib import DigestAlgorithm, LocalFS, Spec, StorageRoot, get_layout, stage_dir


def test_archive_config_reexported_from_domain_module() -> None:
    from ocfl_cli.archive.config import ArchiveConfig as DomainArchiveConfig

    assert ArchiveConfig is DomainArchiveConfig


def _write_config(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


class TestConfigPath:
    def test_env_override(self, monkeypatch, tmp_path):
        target = tmp_path / "custom.toml"
        monkeypatch.setenv("OCFL_CONFIG", str(target))
        assert config_path() == target

    def test_xdg_default(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OCFL_CONFIG", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        assert config_path() == tmp_path / "ocfl" / "config.toml"


class TestConfigParse:
    def test_missing_file_empty(self, tmp_path):
        cfg = load_config(tmp_path / "nope.toml")
        assert cfg == Config()
        assert cfg.archives == {}

    def test_empty_file(self, tmp_path):
        path = _write_config(tmp_path / "c.toml", "")
        cfg = load_config(path)
        assert cfg.defaults_digest is None
        assert cfg.defaults_suppress == ()
        assert cfg.archives == {}

    def test_unknown_keys_ignored(self, tmp_path):
        path = _write_config(
            tmp_path / "c.toml",
            """
            [defaults]
            digest = "sha256"
            color = true

            [archives.a]
            root = "/x"
            custom_field = 42
            """,
        )
        cfg = load_config(path)
        assert cfg.defaults_digest == "sha256"
        assert cfg.archive("a") is not None

    def test_defaults_only(self, tmp_path):
        path = _write_config(
            tmp_path / "c.toml",
            """
            [defaults]
            digest = "sha256"
            suppress = ["W004", "W007"]
            """,
        )
        cfg = load_config(path)
        assert cfg.defaults_digest == "sha256"
        assert cfg.defaults_suppress == ("W004", "W007")
        assert cfg.archives == {}

    def test_archives(self, tmp_path):
        path = _write_config(
            tmp_path / "c.toml",
            """
            [defaults]
            digest = "sha256"
            suppress = ["W004"]

            [archives.bitcoin]
            root = "/srv/ocfl/bitcoin"
            digest = "sha256"
            suppress = ["W004"]

            [archives.manuscripts]
            root = "/srv/ocfl/manuscripts"
            digest = "sha512"
            """,
        )
        cfg = load_config(path)
        assert set(cfg.archives) == {"bitcoin", "manuscripts"}
        btc = cfg.archive("bitcoin")
        assert btc == ArchiveConfig(
            name="bitcoin",
            root="/srv/ocfl/bitcoin",
            digest="sha256",
            suppress=("W004",),
        )
        mss = cfg.archive("manuscripts")
        assert mss is not None
        assert mss.root == "/srv/ocfl/manuscripts"
        assert mss.digest == "sha512"
        assert mss.suppress is None

    def test_archive_invalid_digest_errors(self, tmp_path):
        path = _write_config(
            tmp_path / "c.toml",
            """
            [archives.bad]
            root = "/srv/ocfl/bad"
            digest = "bogus"
            """,
        )
        with pytest.raises(ConfigError, match=r"archives\.bad\.digest"):
            load_config(path)

    def test_archive_missing_root_errors(self, tmp_path):
        path = _write_config(
            tmp_path / "c.toml",
            """
            [archives.bad]
            digest = "sha256"
            """,
        )
        with pytest.raises(ConfigError, match="missing 'root'"):
            load_config(path)

    def test_malformed_toml_errors(self, tmp_path):
        path = _write_config(tmp_path / "c.toml", "not [valid")
        with pytest.raises(ConfigError):
            load_config(path)

    def test_wrong_type_errors(self, tmp_path):
        path = _write_config(
            tmp_path / "c.toml",
            """
            [defaults]
            suppress = "W004"
            """,
        )
        with pytest.raises(ConfigError, match="list of strings"):
            load_config(path)


class TestEffective:
    def test_archive_overrides_defaults(self):
        cfg = Config(
            defaults_digest="sha256",
            defaults_suppress=("W004",),
            archives={
                "m": ArchiveConfig(
                    name="m", root="/x", digest="sha512", suppress=("W007",)
                )
            },
        )
        arch = cfg.archive("m")
        assert cfg.effective_digest(arch) == "sha512"
        assert cfg.effective_suppress(arch) == frozenset({"W007"})

    def test_falls_back_to_defaults(self):
        cfg = Config(
            defaults_digest="sha256",
            defaults_suppress=("W004",),
            archives={
                "m": ArchiveConfig(name="m", root="/x", digest=None, suppress=None)
            },
        )
        arch = cfg.archive("m")
        assert cfg.effective_digest(arch) == "sha256"
        assert cfg.effective_suppress(arch) == frozenset({"W004"})

    def test_empty_suppress_overrides_defaults(self):
        """suppress = [] explicitly opts out of default suppressions."""
        cfg = Config(
            defaults_suppress=("W004",),
            archives={"strict": ArchiveConfig(name="strict", root="/x", suppress=())},
        )
        arch = cfg.archive("strict")
        assert cfg.effective_suppress(arch) == frozenset()

    def test_implicit_default_single_archive(self):
        cfg = Config(archives={"only": ArchiveConfig(name="only", root="/x")})
        assert cfg.implicit_default() is not None
        assert cfg.implicit_default().name == "only"  # type: ignore[union-attr]

    def test_implicit_default_multi_returns_none(self):
        cfg = Config(
            archives={
                "a": ArchiveConfig(name="a", root="/x"),
                "b": ArchiveConfig(name="b", root="/y"),
            }
        )
        assert cfg.implicit_default() is None


class TestAliasResolution:
    def test_alias_resolves_to_root(
        self, runner: CliRunner, storage_root: Path, tmp_path, monkeypatch
    ):
        cfg = _write_config(
            tmp_path / "config.toml",
            f"""
            [archives.mine]
            root = "{storage_root}"
            """,
        )
        monkeypatch.setenv("OCFL_CONFIG", str(cfg))
        result = runner.invoke(app, ["-r", "mine", "validate"])
        assert result.exit_code == 0, result.output
        assert "Valid" in result.stdout

    def test_implicit_single_archive(
        self, runner: CliRunner, storage_root: Path, tmp_path, monkeypatch
    ):
        cfg = _write_config(
            tmp_path / "config.toml",
            f"""
            [archives.only]
            root = "{storage_root}"
            """,
        )
        monkeypatch.setenv("OCFL_CONFIG", str(cfg))
        monkeypatch.delenv("OCFL_ROOT", raising=False)
        result = runner.invoke(app, ["validate"])
        assert result.exit_code == 0, result.output

    def test_env_var_beats_implicit_default(
        self, runner: CliRunner, storage_root: Path, tmp_path, monkeypatch
    ):
        """OCFL_ROOT env takes precedence over implicit single-archive."""
        cfg = _write_config(
            tmp_path / "config.toml",
            """
            [archives.only]
            root = "/does/not/exist"
            """,
        )
        monkeypatch.setenv("OCFL_CONFIG", str(cfg))
        monkeypatch.setenv("OCFL_ROOT", str(storage_root))
        result = runner.invoke(app, ["validate"])
        assert result.exit_code == 0, result.output

    def test_flag_path_still_works_when_not_an_alias(
        self, runner: CliRunner, storage_root: Path, tmp_path, monkeypatch
    ):
        cfg = _write_config(
            tmp_path / "config.toml",
            """
            [archives.other]
            root = "/does/not/exist"
            """,
        )
        monkeypatch.setenv("OCFL_CONFIG", str(cfg))
        result = runner.invoke(app, ["-r", str(storage_root), "validate"])
        assert result.exit_code == 0, result.output


class TestConfigDigestDefault:
    def test_stage_new_rejects_invalid_config_digest(
        self, runner: CliRunner, storage_root: Path, tmp_path, monkeypatch
    ):
        cfg = _write_config(
            tmp_path / "config.toml",
            """
            [defaults]
            digest = "bogus"
            """,
        )
        monkeypatch.setenv("OCFL_CONFIG", str(cfg))

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
                "obj:bad-cfg",
            ],
        )
        assert result.exit_code == 6
        assert "defaults.digest" in result.output
        assert "bogus" in result.output
        assert "traceback" not in result.output.lower()

        from ocfl_cli.stage.store import StagingStore

        store = StagingStore(staging)
        assert not store.exists(str(storage_root.resolve()), "obj:bad-cfg")

    def test_stage_new_uses_config_digest(
        self, runner: CliRunner, storage_root: Path, tmp_path, monkeypatch
    ):
        cfg = _write_config(
            tmp_path / "config.toml",
            """
            [defaults]
            digest = "sha256"
            """,
        )
        monkeypatch.setenv("OCFL_CONFIG", str(cfg))

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
                "obj:cfg",
            ],
        )
        assert result.exit_code == 0, result.output

        from ocfl_cli.stage.store import StagingStore

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        inv = store.load_inventory(root_path, "obj:cfg")
        assert inv.digest_algorithm == "sha256"

    def test_archive_digest_beats_defaults(
        self, runner: CliRunner, storage_root: Path, tmp_path, monkeypatch
    ):
        cfg = _write_config(
            tmp_path / "config.toml",
            f"""
            [defaults]
            digest = "sha512"

            [archives.mine]
            root = "{storage_root}"
            digest = "sha256"
            """,
        )
        monkeypatch.setenv("OCFL_CONFIG", str(cfg))

        staging = tmp_path / "staging"
        result = runner.invoke(
            app,
            [
                "-r",
                "mine",
                "stage",
                "--staging-root",
                str(staging),
                "new",
                "obj:cfg2",
            ],
        )
        assert result.exit_code == 0, result.output

        from ocfl_cli.stage.store import StagingStore

        store = StagingStore(staging)
        root_path = str(storage_root.resolve())
        inv = store.load_inventory(root_path, "obj:cfg2")
        assert inv.digest_algorithm == "sha256"


@pytest.fixture
def sha256_root(tmp_path: Path) -> Path:
    """Storage root using sha256 digest (triggers W004 on validate)."""
    fs = LocalFS(tmp_path)
    layout = get_layout("0002-flat-direct-storage-layout")
    root = StorageRoot.init(fs, fs, spec=Spec.V1_1, layout=layout)

    obj_dir = tmp_path / "files"
    obj_dir.mkdir()
    (obj_dir / "a.txt").write_text("x")
    stage = stage_dir(fs, "files", DigestAlgorithm.SHA256)
    root.commit(fs, "obj:x", stage, message="v1")
    shutil.rmtree(obj_dir)
    return tmp_path


class TestSuppressWarnings:
    def test_w004_emitted_without_suppress(self, runner: CliRunner, sha256_root: Path):
        result = runner.invoke(
            app, ["--root", str(sha256_root), "--json", "validate", "obj:x"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        codes = [w["code"] for w in data["warnings"]]
        assert "W004" in codes

    def test_w004_suppressed_via_defaults(
        self, runner: CliRunner, sha256_root: Path, tmp_path, monkeypatch
    ):
        cfg = _write_config(
            tmp_path / "config.toml",
            """
            [defaults]
            suppress = ["W004"]
            """,
        )
        monkeypatch.setenv("OCFL_CONFIG", str(cfg))
        result = runner.invoke(
            app, ["--root", str(sha256_root), "--json", "validate", "obj:x"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        codes = [w["code"] for w in data["warnings"]]
        assert "W004" not in codes

    def test_suppress_via_archive_alias(
        self, runner: CliRunner, sha256_root: Path, tmp_path, monkeypatch
    ):
        cfg = _write_config(
            tmp_path / "config.toml",
            f"""
            [archives.mine]
            root = "{sha256_root}"
            suppress = ["W004"]
            """,
        )
        monkeypatch.setenv("OCFL_CONFIG", str(cfg))
        result = runner.invoke(app, ["-r", "mine", "--json", "validate", "obj:x"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        codes = [w["code"] for w in data["warnings"]]
        assert "W004" not in codes
