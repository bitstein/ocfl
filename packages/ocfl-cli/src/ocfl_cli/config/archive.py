from pydantic import BaseModel, ConfigDict, field_validator

from ocfl_lib import DigestAlgorithm

__all__ = ["ArchiveConfig", "validate_digest_name"]


def validate_digest_name(value: str) -> str:
    alg = DigestAlgorithm.from_ocfl_name(value)
    if not alg.is_spec_algorithm:
        raise ValueError("content digest must be sha512 or sha256")
    return value


class ArchiveConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    root: str
    digest: str | None = None
    # None = inherit defaults, () = suppress nothing
    suppress: tuple[str, ...] | None = None

    @field_validator("digest")
    @classmethod
    def _digest(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return validate_digest_name(v)
