"""User configuration for the ocfl CLI (XDG-located config.toml)."""

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from platformdirs import user_config_path

from ocfl_cli.errors import ConfigError

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


@dataclass(frozen=True)
class ArchiveConfig:
    """One `[archives.<name>]` section."""

    name: str
    root: str
    digest: str | None = None
    # None = inherit defaults, () = suppress nothing
    suppress: tuple[str, ...] | None = None


@dataclass(frozen=True)
class SimpleProofConfig:
    """`[simpleproof]` section. Secrets are repr=False to keep logs safe."""

    base_url: str
    proof_category_id: int
    access_token: str = field(repr=False, default="")
    api_key: str = field(repr=False, default="")
    timeout_s: int = 30

    def __post_init__(self) -> None:
        _validate_http_url(self.base_url, "simpleproof.base_url")
        _require_int(self.proof_category_id, "simpleproof.proof_category_id")
        _require_pos_int(self.timeout_s, "simpleproof.timeout_s")
        _require_str(self.access_token, "simpleproof.access_token")
        _require_str(self.api_key, "simpleproof.api_key")


@dataclass(frozen=True)
class BitcoinConfig:
    """`[bitcoin]` section. `rpc_password` is repr=False."""

    rpc_url: str
    zmq_endpoint: str
    rpc_cookie: Path | None = None
    rpc_user: str = ""
    rpc_password: str = field(repr=False, default="")
    confirmations: int = 3
    rpc_timeout_s: int = 10

    def __post_init__(self) -> None:
        _validate_http_url(self.rpc_url, "bitcoin.rpc_url")
        _validate_zmq_endpoint(self.zmq_endpoint, "bitcoin.zmq_endpoint")
        if self.rpc_cookie is not None and not isinstance(self.rpc_cookie, Path):
            raise ConfigError("bitcoin.rpc_cookie must be a path")
        _require_str(self.rpc_user, "bitcoin.rpc_user")
        _require_str(self.rpc_password, "bitcoin.rpc_password")
        _require_pos_int(self.rpc_timeout_s, "bitcoin.rpc_timeout_s")

        has_cookie = self.rpc_cookie is not None
        has_user = bool(self.rpc_user)
        has_pass = bool(self.rpc_password)
        if has_user != has_pass:
            raise ConfigError(
                "bitcoin: rpc_user and rpc_password must both be set or both unset"
            )
        if has_cookie and has_user:
            raise ConfigError(
                "bitcoin: rpc_cookie and rpc_user/rpc_password are mutually exclusive"
            )
        if not (has_cookie or has_user):
            raise ConfigError(
                "bitcoin: must configure rpc_cookie or rpc_user+rpc_password"
            )

        _require_int(self.confirmations, "bitcoin.confirmations")
        if not 1 <= self.confirmations <= 100:
            raise ConfigError(
                f"bitcoin.confirmations must be in 1..100 (got {self.confirmations})"
            )
        if self.confirmations < 3:
            logger.warning(
                "bitcoin.confirmations=%d is shallow; >=3 recommended to "
                "tolerate reorgs",
                self.confirmations,
            )


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
    cfg = _parse(data, path)
    _maybe_warn_loose_perms(path, cfg)
    return cfg


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
        suppress = _opt_str_list(section, "suppress", path, f"archives.{name}.suppress")
        archives[name] = ArchiveConfig(
            name=name, root=root, digest=digest, suppress=suppress
        )

    simpleproof = _parse_simpleproof(data.get("simpleproof"), path)
    bitcoin = _parse_bitcoin(data.get("bitcoin"), path)

    return Config(
        path=path,
        defaults_digest=d_digest,
        defaults_suppress=d_suppress or (),
        archives=archives,
        simpleproof=simpleproof,
        bitcoin=bitcoin,
    )


def _parse_simpleproof(section: Any, path: Path) -> SimpleProofConfig | None:
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ConfigError(f"{path}: [simpleproof] must be a table")
    d: dict[str, Any] = dict(section)
    for env_key, field_name in (
        ("SIMPLEPROOF_BASE_URL", "base_url"),
        ("SIMPLEPROOF_ACCESS_TOKEN", "access_token"),
        ("SIMPLEPROOF_API_KEY", "api_key"),
    ):
        env_val = os.environ.get(env_key)
        if env_val is not None:
            d[field_name] = env_val

    base_url = _req_str(d, "base_url", path, "simpleproof.base_url")
    proof_id = _req_int(d, "proof_category_id", path, "simpleproof.proof_category_id")
    access = _opt_str(d, "access_token", path, "simpleproof.access_token") or ""
    api_key = _opt_str(d, "api_key", path, "simpleproof.api_key") or ""
    timeout_s = _opt_int(d, "timeout_s", path, "simpleproof.timeout_s")
    return SimpleProofConfig(
        base_url=base_url,
        proof_category_id=proof_id,
        access_token=access,
        api_key=api_key,
        timeout_s=30 if timeout_s is None else timeout_s,
    )


def _parse_bitcoin(section: Any, path: Path) -> BitcoinConfig | None:
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ConfigError(f"{path}: [bitcoin] must be a table")
    d: dict[str, Any] = dict(section)

    rpc_url = _req_str(d, "rpc_url", path, "bitcoin.rpc_url")
    zmq_endpoint = _req_str(d, "zmq_endpoint", path, "bitcoin.zmq_endpoint")
    cookie_str = _opt_str(d, "rpc_cookie", path, "bitcoin.rpc_cookie")
    rpc_user = _opt_str(d, "rpc_user", path, "bitcoin.rpc_user") or ""
    rpc_password = _opt_str(d, "rpc_password", path, "bitcoin.rpc_password") or ""
    confirmations = _opt_int(d, "confirmations", path, "bitcoin.confirmations")
    rpc_timeout_s = _opt_int(d, "rpc_timeout_s", path, "bitcoin.rpc_timeout_s")

    cookie = Path(cookie_str).expanduser() if cookie_str is not None else None
    return BitcoinConfig(
        rpc_url=rpc_url,
        zmq_endpoint=zmq_endpoint,
        rpc_cookie=cookie,
        rpc_user=rpc_user,
        rpc_password=rpc_password,
        confirmations=3 if confirmations is None else confirmations,
        rpc_timeout_s=10 if rpc_timeout_s is None else rpc_timeout_s,
    )


def _split(value: str, label: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise ConfigError(f"{label} must be a string")
    try:
        parsed = urlsplit(value)
    except ValueError as e:
        raise ConfigError(f"{label}: {e}") from None
    return parsed.scheme, parsed.netloc


def _validate_http_url(value: str, label: str) -> None:
    scheme, netloc = _split(value, label)
    if scheme not in ("http", "https"):
        raise ConfigError(f"{label} must be an http(s) URL (got {value!r})")
    if not netloc:
        raise ConfigError(f"{label} must include a host (got {value!r})")


def _validate_zmq_endpoint(value: str, label: str) -> None:
    scheme, _ = _split(value, label)
    if scheme not in ("tcp", "ipc"):
        raise ConfigError(f"{label} must use tcp:// or ipc:// scheme (got {value!r})")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_str(value: Any, label: str) -> None:
    if not isinstance(value, str):
        raise ConfigError(f"{label} must be a string")


def _require_int(value: Any, label: str) -> None:
    if not _is_int(value):
        raise ConfigError(f"{label} must be an integer")


def _require_pos_int(value: Any, label: str) -> None:
    _require_int(value, label)
    if value <= 0:
        raise ConfigError(f"{label} must be positive")


def _has_secret(cfg: Config) -> bool:
    sp = cfg.simpleproof
    if sp is not None and (sp.access_token or sp.api_key):
        return True
    btc = cfg.bitcoin
    return bool(btc is not None and btc.rpc_password)


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


def _opt_str(tbl: dict[str, Any], key: str, path: Path, label: str) -> str | None:
    v = tbl.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise ConfigError(f"{path}: {label} must be a string")
    return v


def _req_str(tbl: dict[str, Any], key: str, path: Path, label: str) -> str:
    v = tbl.get(key)
    if v is None:
        raise ConfigError(f"{path}: {label} is required")
    if not isinstance(v, str):
        raise ConfigError(f"{path}: {label} must be a string")
    return v


def _opt_int(tbl: dict[str, Any], key: str, path: Path, label: str) -> int | None:
    v = tbl.get(key)
    if v is None:
        return None
    if not _is_int(v):
        raise ConfigError(f"{path}: {label} must be an integer")
    return v


def _req_int(tbl: dict[str, Any], key: str, path: Path, label: str) -> int:
    v = tbl.get(key)
    if v is None:
        raise ConfigError(f"{path}: {label} is required")
    if not _is_int(v):
        raise ConfigError(f"{path}: {label} must be an integer")
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
