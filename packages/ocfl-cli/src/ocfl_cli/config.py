"""User configuration for the ocfl CLI (XDG-located config.toml)."""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from platformdirs import user_config_path


@dataclass(frozen=True)
class ArchiveConfig:
    """One `[archives.<name>]` section."""

    name: str
    root: str
    digest: str | None = None
    # None = inherit defaults, () = suppress nothing
    suppress: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Config:
    """Loaded user config. `path is None` when no file existed."""

    path: Path | None = None
    defaults_digest: str | None = None
    defaults_suppress: tuple[str, ...] = ()
    archives: dict[str, ArchiveConfig] = field(default_factory=dict)

    def archive(self, name: str) -> ArchiveConfig | None:
        return self.archives.get(name)

    def implicit_default(self) -> ArchiveConfig | None:
        """The sole archive if exactly one is configured, else None."""
        if len(self.archives) == 1:
            return next(iter(self.archives.values()))
        return None

    def effective_digest(self, archive: ArchiveConfig | None) -> str | None:
        if archive is not None and archive.digest is not None:
            return archive.digest
        return self.defaults_digest

    def effective_suppress(
        self, archive: ArchiveConfig | None
    ) -> frozenset[str]:
        if archive is not None and archive.suppress is not None:
            return frozenset(archive.suppress)
        return frozenset(self.defaults_suppress)


class ConfigError(ValueError):
    """Raised when the config file is malformed."""


def config_path() -> Path:
    """Return the canonical config path, honoring `$OCFL_CONFIG` then XDG."""
    override = os.environ.get("OCFL_CONFIG")
    if override:
        return Path(override)
    return user_config_path("ocfl") / "config.toml"


def load_config(path: Path | None = None) -> Config:
    """Load config from *path* (default: `config_path()`). Missing file → empty."""
    if path is None:
        path = config_path()
    if not path.is_file():
        return Config()
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    except OSError as e:
        raise ConfigError(f"{path}: {e}") from None
    return _parse(data, path)


def _parse(data: dict[str, Any], path: Path) -> Config:
    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ConfigError(f"{path}: [defaults] must be a table")
    d_digest = _opt_str(defaults, "digest", path, "defaults.digest")
    d_suppress = _opt_str_list(defaults, "suppress", path, "defaults.suppress")

    archives_tbl = data.get("archives", {})
    if not isinstance(archives_tbl, dict):
        raise ConfigError(f"{path}: [archives] must be a table")

    archives: dict[str, ArchiveConfig] = {}
    for name, section in archives_tbl.items():
        if not isinstance(section, dict):
            raise ConfigError(f"{path}: [archives.{name}] must be a table")
        root = _opt_str(section, "root", path, f"archives.{name}.root")
        if not root:
            raise ConfigError(f"{path}: archives.{name} missing 'root'")
        digest = _opt_str(section, "digest", path, f"archives.{name}.digest")
        suppress = _opt_str_list(
            section, "suppress", path, f"archives.{name}.suppress"
        )
        archives[name] = ArchiveConfig(
            name=name, root=root, digest=digest, suppress=suppress
        )

    return Config(
        path=path,
        defaults_digest=d_digest,
        defaults_suppress=d_suppress or (),
        archives=archives,
    )


def _opt_str(
    tbl: dict[str, Any], key: str, path: Path, label: str
) -> str | None:
    v = tbl.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise ConfigError(f"{path}: {label} must be a string")
    return v


def _opt_str_list(
    tbl: dict[str, Any], key: str, path: Path, label: str
) -> tuple[str, ...] | None:
    v = tbl.get(key)
    if v is None:
        return None
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ConfigError(f"{path}: {label} must be a list of strings")
    return tuple(v)
