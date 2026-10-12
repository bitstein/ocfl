"""The NNNN-ots-timestamps extension: proofs, config and content-proof rules.

Source of truth: docs/ext/NNNN-ots-timestamps.md. Everything here is pure;
the base install of ocfl-lib has no network code.
"""

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from opentimestamps.core.notary import BitcoinBlockHeaderAttestation
from opentimestamps.core.serialize import (
    BytesDeserializationContext,
    DeserializationError,
)
from opentimestamps.core.timestamp import DetachedTimestampFile

from ocfl_lib._errors import OtsConfigError, OtsParseError

if TYPE_CHECKING:
    from collections.abc import Iterable

EXTENSION_NAME = "NNNN-ots-timestamps"
PROOF_SUFFIX = ".ots"


# --- proofs


@dataclass(frozen=True, slots=True)
class ParsedOts:
    """A parsed proof.

    `file_hash` names the proof's file-hash operation (`sha256`, `sha1`, ...);
    `commitment_digest` is the file digest it commits to. `block_height` and
    `attested_digest` are those of the lowest Bitcoin attestation, or None
    for a pending proof.
    """

    commitment_digest: bytes
    attested_digest: bytes | None
    block_height: int | None
    file_hash: str = "sha256"

    @property
    def is_final(self) -> bool:
        """True when the proof holds a Bitcoin attestation."""
        return self.block_height is not None


def parse(ots_bytes: bytes) -> ParsedOts:
    """Parse ``.ots`` bytes; raise :class:`OtsParseError` on failure."""
    ctx = BytesDeserializationContext(ots_bytes)
    try:
        detached = DetachedTimestampFile.deserialize(ctx)
    except (DeserializationError, ValueError) as exc:
        raise OtsParseError("failed to deserialize .ots bytes") from exc

    bitcoin_attestations: list[tuple[int, bytes]] = []
    for msg, attestation in detached.timestamp.all_attestations():
        if isinstance(attestation, BitcoinBlockHeaderAttestation):
            bitcoin_attestations.append((attestation.height, msg))

    if bitcoin_attestations:
        block_height, attested_digest = min(bitcoin_attestations, key=lambda x: x[0])
    else:
        block_height = None
        attested_digest = None

    return ParsedOts(
        commitment_digest=detached.timestamp.msg,
        attested_digest=attested_digest,
        block_height=block_height,
        file_hash=detached.file_hash_op.TAG_NAME,
    )


# --- config

_PARAMS: dict[str, type] = {
    "proofDigestAlgorithm": str,
    "timestampContent": bool,
    "timestampInventory": bool,
    "timestampFrom": str,
}


@dataclass(frozen=True, slots=True)
class OtsConfig:
    """The parameters of the extension's `config.json`.

    `unknown` names the properties the extension does not define (W904).
    `proof_digest_algorithm` may hold any string here; validation reports
    one that is not `sha256` (E901).
    """

    proof_digest_algorithm: str = "sha256"
    timestamp_content: bool = True
    timestamp_inventory: bool = True
    timestamp_from: str | None = None
    unknown: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, raw: bytes) -> OtsConfig:
        """Parse `config.json` bytes. Raises OtsConfigError (E908)."""
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise OtsConfigError(f"config.json is not JSON: {e}") from e
        if not isinstance(data, dict):
            raise OtsConfigError("config.json is not a JSON object")
        if data.get("extensionName") != EXTENSION_NAME:
            raise OtsConfigError(
                f"config.json extensionName is {data.get('extensionName')!r}, "
                f"not {EXTENSION_NAME!r}"
            )
        for key, typ in _PARAMS.items():
            if key in data and type(data[key]) is not typ:
                raise OtsConfigError(
                    f"config.json {key} must be a {typ.__name__}, "
                    f"not {type(data[key]).__name__}"
                )
        known = {"extensionName", *_PARAMS}
        return cls(
            proof_digest_algorithm=data.get("proofDigestAlgorithm", "sha256"),
            timestamp_content=data.get("timestampContent", True),
            timestamp_inventory=data.get("timestampInventory", True),
            timestamp_from=data.get("timestampFrom"),
            unknown=tuple(sorted(k for k in data if k not in known)),
        )

    def to_json(self) -> bytes:
        """`config.json` bytes; `timestampFrom` only when set."""
        data: dict[str, str | bool] = {
            "extensionName": EXTENSION_NAME,
            "proofDigestAlgorithm": self.proof_digest_algorithm,
            "timestampContent": self.timestamp_content,
            "timestampInventory": self.timestamp_inventory,
        }
        if self.timestamp_from is not None:
            data["timestampFrom"] = self.timestamp_from
        return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


# --- content proofs


def split_state(paths: Iterable[str]) -> tuple[frozenset[str], dict[str, str]]:
    """(ordinary paths, {content proof: companion}) of one version's state.

    `P.ots` is a content proof when `P` is in the same state. Every other
    path, including a standalone `x.ots`, is ordinary and needs a proof.
    """
    path_set = set(paths)
    proofs = {
        p: p.removesuffix(PROOF_SUFFIX)
        for p in path_set
        if p.endswith(PROOF_SUFFIX) and p.removesuffix(PROOF_SUFFIX) in path_set
    }
    return frozenset(path_set - proofs.keys()), proofs
