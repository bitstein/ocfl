"""The NNNN-ots-timestamps extension: proofs, config and content-proof rules.

Source of truth: docs/ext/NNNN-ots-timestamps.md. Everything here is pure;
the base install of ocfl-lib has no network code.
"""

import hashlib
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
from ocfl_lib._json_value import as_object, object_field, parse_json
from ocfl_lib._path import join_path
from ocfl_lib.fs import _utils as fs_utils

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ocfl_lib.fs._protocols import FS

EXTENSION_NAME = "NNNN-ots-timestamps"
EXTENSION_DIR = f"extensions/{EXTENSION_NAME}"
"""The extension's directory, relative to the object root."""
CONFIG_PATH = f"{EXTENSION_DIR}/config.json"
PROOF_SUFFIX = ".ots"


def inventory_proof_path(version: str) -> str:
    """Object-relative path of the inventory proof of `version`."""
    return f"{EXTENSION_DIR}/inventory.json.{version}{PROOF_SUFFIX}"


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


def read_config(fsys: FS, obj_path: str) -> OtsConfig | None:
    """The object's extension config; None when the extension is not in use.

    The extension is in use when its directory exists, so a directory
    without a valid `config.json` raises OtsConfigError (E908).
    """
    if not fsys.exists(join_path(obj_path, EXTENSION_DIR)):
        return None
    try:
        raw = fs_utils.read_all(fsys, join_path(obj_path, CONFIG_PATH))
    except FileNotFoundError as e:
        raise OtsConfigError(f"{obj_path}: {EXTENSION_DIR} has no config.json") from e
    return OtsConfig.from_json(raw)


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


def covered_versions(cfg: OtsConfig, versions: Iterable[str]) -> list[str]:
    """The versions from `timestampFrom` on, oldest first.

    Raises OtsConfigError when `timestampFrom` names no version (E908).
    """
    ordered = sorted(versions, key=lambda v: int(v[1:]))
    if cfg.timestamp_from is None:
        return ordered
    if cfg.timestamp_from not in ordered:
        raise OtsConfigError(
            f"timestampFrom {cfg.timestamp_from!r} names no version of the object"
        )
    return ordered[ordered.index(cfg.timestamp_from) :]


# --- the proofs of an object


@dataclass(frozen=True, slots=True)
class ProofRef:
    """One proof of an object and the sha256 hex digest it must commit to.

    `kind` is "content" or "inventory"; `path` is relative to the object
    root; `version` is the first covered version that holds the proof.
    """

    kind: str
    version: str
    path: str
    digest: str


def object_proofs(fsys: FS, obj_path: str) -> tuple[ProofRef, ...]:
    """Every proof of the covered versions of the object at `obj_path`.

    Content proofs (when `timestampContent`) come from the root inventory's
    states, each content file once; inventory proofs are those present for
    covered versions. Empty when the extension is not in use. Raises
    OtsConfigError for an invalid config.
    """
    cfg = read_config(fsys, obj_path)
    if cfg is None:
        return ()
    inv = as_object(
        parse_json(fs_utils.read_all(fsys, join_path(obj_path, "inventory.json")))
    )
    if inv is None:
        return ()
    versions = object_field(inv, "versions")
    covered = covered_versions(cfg, versions)
    refs: list[ProofRef] = []
    if cfg.timestamp_content:
        first_path = {
            d: ps[0]
            for d, ps in object_field(inv, "manifest").items()
            if isinstance(ps, list) and ps and isinstance(ps[0], str)
        }
        seen: set[str] = set()
        for v in covered:
            state = object_field(as_object(versions.get(v)) or {}, "state")
            digest_of = {
                p: d
                for d, ps in state.items()
                if isinstance(ps, list)
                for p in ps
                if isinstance(p, str)
            }
            _ordinary, proofs = split_state(digest_of)
            for proof, companion in sorted(proofs.items()):
                path = first_path.get(digest_of[proof])
                if path is None or path in seen:
                    continue
                seen.add(path)
                refs.append(ProofRef("content", v, path, digest_of[companion]))
    for v in covered:
        path = inventory_proof_path(v)
        if not fsys.exists(join_path(obj_path, path)):
            continue
        try:
            raw = fs_utils.read_all(fsys, join_path(obj_path, v, "inventory.json"))
        except FileNotFoundError:
            continue  # E909
        refs.append(ProofRef("inventory", v, path, hashlib.sha256(raw).hexdigest()))
    return tuple(refs)
