"""User configuration for the ocfl CLI (XDG-located config.toml)."""

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Self
from urllib.parse import urlsplit

from platformdirs import user_config_path
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
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
    "config_path",
    "load_config",
]

logger = logging.getLogger(__name__)


def _validate_http_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"must be an http(s) URL (got {value!r})")
    if not parsed.netloc:
        raise ValueError(f"must include a host (got {value!r})")
    return value


def _validate_zmq_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in ("tcp", "ipc"):
        raise ValueError(f"must use tcp:// or ipc:// scheme (got {value!r})")
    return value


class _EnvOverInitSettings:
    """Mixin: rank env_settings above init kwargs so env beats TOML."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (env_settings, init_settings, dotenv_settings, file_secret_settings)


class ArchiveConfig(BaseModel):
    """One `[archives.<name>]` section."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    root: str
    digest: str | None = None
    # None = inherit defaults, () = suppress nothing
    suppress: tuple[str, ...] | None = None


class SimpleProofConfig(_EnvOverInitSettings, BaseSettings):
    """`[simpleproof]` section. Tokens are SecretStr."""

    model_config = SettingsConfigDict(
        frozen=True,
        extra="ignore",
        env_prefix="SIMPLEPROOF_",
    )

    base_url: str
    proof_category_id: int
    access_token: SecretStr = SecretStr("")
    api_key: SecretStr = SecretStr("")
    timeout_s: Annotated[int, Field(gt=0)] = 30

    @field_validator("base_url")
    @classmethod
    def _http(cls, v: str) -> str:
        return _validate_http_url(v)


class BitcoinConfig(_EnvOverInitSettings, BaseSettings):
    """`[bitcoin]` section. `rpc_password` is SecretStr."""

    model_config = SettingsConfigDict(
        frozen=True,
        extra="ignore",
        env_prefix="BITCOIN_",
    )

    rpc_url: str
    zmq_endpoint: str
    rpc_user: Annotated[str, Field(min_length=1)]
    rpc_password: Annotated[SecretStr, Field(min_length=1)]
    confirmations: Annotated[int, Field(ge=1, le=100)] = 3
    rpc_timeout_s: Annotated[int, Field(gt=0)] = 10

    @field_validator("rpc_url")
    @classmethod
    def _http(cls, v: str) -> str:
        return _validate_http_url(v)

    @field_validator("zmq_endpoint")
    @classmethod
    def _zmq(cls, v: str) -> str:
        return _validate_zmq_endpoint(v)

    @model_validator(mode="after")
    def _warn_shallow_confirmations(self) -> Self:
        if self.confirmations < 3:
            logger.warning(
                "bitcoin.confirmations=%d is shallow; >=3 recommended to "
                "tolerate reorgs",
                self.confirmations,
            )
        return self


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
    if not path.is_file():
        return Config()
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    except OSError as e:
        raise ConfigError(f"{path}: {e}") from None
    cfg = _build(data, path)
    _maybe_warn_loose_perms(path, cfg)
    return cfg


def _build(data: dict[str, Any], path: Path) -> Config:
    defaults = _require_table(data.get("defaults", {}), path, "defaults")
    d_digest = _opt_str(defaults, "digest", path, "defaults.digest")
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
        SimpleProofConfig, data.get("simpleproof"), path, "simpleproof"
    )
    bitcoin = _build_section(BitcoinConfig, data.get("bitcoin"), path, "bitcoin")

    return Config(
        path=path,
        defaults_digest=d_digest,
        defaults_suppress=d_suppress or (),
        archives=archives,
        simpleproof=simpleproof,
        bitcoin=bitcoin,
    )


def _build_section[T: BaseSettings](
    cls: type[T], section: Any, path: Path, label: str
) -> T | None:
    if section is None:
        return None
    section = _require_table(section, path, label)
    return _validate(cls, section, path, label)


def _validate[T: BaseModel](
    cls: type[T], data: dict[str, Any], path: Path, label: str
) -> T:
    try:
        return cls(**data)
    except ValidationError as e:
        raise ConfigError(_format_validation_error(e, path, label)) from None


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


def _has_secret(cfg: Config) -> bool:
    sp = cfg.simpleproof
    if sp is not None and (
        sp.access_token.get_secret_value() or sp.api_key.get_secret_value()
    ):
        return True
    btc = cfg.bitcoin
    return bool(btc is not None and btc.rpc_password.get_secret_value())


def _maybe_warn_loose_perms(path: Path, cfg: Config) -> None:
    if not _has_secret(cfg):
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & 0o077:
        logger.warning(
            "config file %s has loose permissions (mode %o); "
            "secrets present — chmod 600 recommended",
            path,
            mode & 0o777,
        )
