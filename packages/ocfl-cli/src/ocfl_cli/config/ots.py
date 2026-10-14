import ipaddress
import logging
import os
from pathlib import Path
from typing import Annotated, Self
from urllib.parse import urlsplit

from platformdirs import user_config_path
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

__all__ = [
    "BitcoinConfig",
    "SimpleProofConfig",
    "env_file_path",
    "user_env_file_path",
]

logger = logging.getLogger(__name__)

_PRIVATE_HOST_SUFFIXES = (".ts.net", ".local")
_TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def user_env_file_path() -> Path:
    return user_config_path("ocfl") / ".env"


def env_file_path() -> Path | None:
    override = os.environ.get("OCFL_ENV_FILE")
    if override:
        return Path(override)
    default = user_env_file_path()
    return default if default.is_file() else None


def _is_private_host(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return host.lower().endswith(_PRIVATE_HOST_SUFFIXES)
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip in _TAILSCALE_CGNAT


def _validate_http_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"must be an http(s) URL (got {value!r})")
    if not parsed.hostname:
        raise ValueError(f"must include a host (got {value!r})")
    if parsed.scheme == "http" and not _is_private_host(parsed.hostname):
        raise ValueError(
            "http:// only allowed for loopback/private/Tailscale hosts; "
            f"use https:// for {parsed.hostname!r}"
        )
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
        dotenv_settings = DotEnvSettingsSource(
            settings_cls,
            env_file=env_file_path(),
            env_file_encoding="utf-8",
            dotenv_filtering="match_prefix",
        )
        return (env_settings, dotenv_settings, init_settings, file_secret_settings)


class SimpleProofConfig(_EnvOverInitSettings, BaseSettings):
    model_config = SettingsConfigDict(
        frozen=True,
        extra="forbid",
        env_prefix="SIMPLEPROOF_",
    )

    base_url: str
    proof_category_id: int
    access_token: Annotated[SecretStr, Field(min_length=1)]
    api_key: Annotated[SecretStr, Field(min_length=1)]
    timeout_s: Annotated[int, Field(gt=0)] = 30

    @field_validator("base_url")
    @classmethod
    def _http(cls, v: str) -> str:
        return _validate_http_url(v)


class BitcoinConfig(_EnvOverInitSettings, BaseSettings):
    model_config = SettingsConfigDict(
        frozen=True,
        extra="forbid",
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
