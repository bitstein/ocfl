"""User configuration for the ocfl CLI (XDG-located config.toml)."""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from platformdirs import user_config_path
from pydantic import BaseModel, ValidationError
from pydantic_settings import BaseSettings

from ocfl_cli.config.archive import ArchiveConfig, validate_digest_name
from ocfl_cli.config.ots import (
    BitcoinConfig,
    SimpleProofConfig,
    env_file_path,
    user_env_file_path,
)
from ocfl_cli.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "ArchiveConfig",
    "BitcoinConfig",
    "Config",
    "ConfigError",
    "SimpleProofConfig",
    "check_env_file",
    "config_path",
    "env_file_path",
    "load_config",
    "redacted",
    "user_env_file_path",
]

# Values of these keys are secrets: `redacted` never shows them.
_SECRET_KEYS = frozenset({"access_token", "api_key", "rpc_user", "rpc_password"})
REDACTED = "<redacted>"


@dataclass(frozen=True)
class Config:
    """Loaded user config. `path is None` when no file existed."""

    path: Path | None = None
    defaults_digest: str | None = None
    defaults_suppress: tuple[str, ...] = ()
    archives: dict[str, ArchiveConfig] = field(default_factory=dict)
    simpleproof: SimpleProofConfig | None = None
    bitcoin: BitcoinConfig | None = None

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

    def effective_suppress(self, archive: ArchiveConfig | None) -> frozenset[str]:
        if archive is not None and archive.suppress is not None:
            return frozenset(archive.suppress)
        return frozenset(self.defaults_suppress)


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
    check_env_file()
    if not path.is_file():
        return Config()
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    except OSError as e:
        raise ConfigError(f"{path}: {e}") from None
    return _build(data, path)


def _build(data: dict[str, Any], path: Path) -> Config:
    defaults = _require_table(data.get("defaults", {}), path, "defaults")
    d_digest = _opt_str(defaults, "digest", path, "defaults.digest")
    if d_digest is not None:
        try:
            d_digest = validate_digest_name(d_digest)
        except ValueError as e:
            raise ConfigError(f"{path}: defaults.digest: {e}") from None
    d_suppress = _opt_str_list(defaults, "suppress", path, "defaults.suppress")

    archives_tbl = _require_table(data.get("archives", {}), path, "archives")
    archives: dict[str, ArchiveConfig] = {}
    for name, section in archives_tbl.items():
        section = _require_table(section, path, f"archives.{name}")
        if "root" not in section:
            raise ConfigError(f"{path}: archives.{name} missing 'root'")
        archives[name] = _validate(
            ArchiveConfig,
            {"name": name, **section},
            path,
            f"archives.{name}",
        )

    simpleproof = _build_section(
        SimpleProofConfig,
        data.get("simpleproof"),
        path,
        "simpleproof",
        forbidden_keys=("access_token", "api_key"),
    )
    bitcoin = _build_section(
        BitcoinConfig,
        data.get("bitcoin"),
        path,
        "bitcoin",
        forbidden_keys=("rpc_user", "rpc_password"),
    )

    return Config(
        path=path,
        defaults_digest=d_digest,
        defaults_suppress=d_suppress or (),
        archives=archives,
        simpleproof=simpleproof,
        bitcoin=bitcoin,
    )


def _build_section[T: BaseSettings](
    cls: type[T],
    section: Any,
    path: Path,
    label: str,
    *,
    forbidden_keys: tuple[str, ...] = (),
) -> T | None:
    if section is None:
        return None
    section = _require_table(section, path, label)
    _reject_forbidden_keys(cls, section, path, label, forbidden_keys)
    return _validate(cls, section, path, label)


def _validate[T: BaseModel](
    cls: type[T], data: dict[str, Any], path: Path, label: str
) -> T:
    try:
        return cls(**data)
    except ValidationError as e:
        raise ConfigError(_format_validation_error(e, path, label)) from None


def _reject_forbidden_keys[T: BaseSettings](
    cls: type[T],
    section: dict[str, Any],
    path: Path,
    label: str,
    forbidden_keys: tuple[str, ...],
) -> None:
    prefix = cls.model_config.get("env_prefix", "")
    for key in forbidden_keys:
        if key in section:
            env_name = f"{prefix}{key}".upper()
            raise ConfigError(
                f"{path}: {label}.{key} is secret and must be set via {env_name}, "
                "not config.toml"
            )


def check_env_file() -> None:
    """Refuse a secrets file that is missing (when named) or not mode 0600."""
    path = env_file_path()
    if path is None:
        return
    if not path.is_file():
        if os.environ.get("OCFL_ENV_FILE"):
            raise ConfigError(f"{path}: OCFL_ENV_FILE does not exist")
        return
    try:
        mode = path.stat().st_mode
    except OSError as e:
        raise ConfigError(f"{path}: {e}") from None
    if mode & 0o077:
        raise ConfigError(
            f"{path}: mode {mode & 0o777:o} is too permissive for a secrets file; "
            f"chmod 600 {path}"
        )


def redacted(cfg: Config) -> dict[str, Any]:
    """The effective config as plain data, every secret replaced by `REDACTED`."""

    def section(model: BaseModel | None) -> dict[str, Any] | None:
        if model is None:
            return None
        data = model.model_dump(mode="json")
        return {k: REDACTED if k in _SECRET_KEYS else v for k, v in data.items()}

    return {
        "path": str(cfg.path) if cfg.path is not None else None,
        "defaults": {
            "digest": cfg.defaults_digest,
            "suppress": list(cfg.defaults_suppress),
        },
        "archives": {name: section(archive) for name, archive in cfg.archives.items()},
        "simpleproof": section(cfg.simpleproof),
        "bitcoin": section(cfg.bitcoin),
    }


def _format_validation_error(e: ValidationError, path: Path, label: str) -> str:
    parts: list[str] = []
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        parts.append(f"{label}.{loc}: {_clean_msg(err)}")
    return f"{path}: " + "; ".join(parts)


def _clean_msg(err: Mapping[str, Any]) -> str:
    """Make pydantic's default messages read like the rest of our errors."""
    # Map pydantic error types to phrasing that reads as a continuation of
    # the "<section>.<field>: " label. Fall through to the raw `msg` for
    # anything we haven't tuned, after stripping the "Value error, " prefix
    # pydantic adds to ValueErrors raised inside validators.
    match err["type"]:
        case "missing":
            return "is required"
        case "string_too_short" | "too_short" if (
            err.get("ctx", {}).get("min_length") == 1
        ):
            return "must not be empty"
        case _:
            return err["msg"].removeprefix("Value error, ")


def _require_table(value: Any, path: Path, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: [{label}] must be a table")
    return value


def _opt_str(tbl: dict[str, Any], key: str, path: Path, label: str) -> str | None:
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
