"""OTS-domain configuration models."""

import logging
from typing import Annotated, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

__all__ = ["BitcoinConfig", "SimpleProofConfig"]

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
