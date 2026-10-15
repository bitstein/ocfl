"""The NNNN-ots-timestamps extension: proofs, config and content-proof rules.

Source of truth: docs/ext/NNNN-ots-timestamps.md. Everything here is pure;
the base install of ocfl-lib has no network code.
"""

import enum
import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, NamedTuple, Protocol, runtime_checkable

from opentimestamps.core.notary import BitcoinBlockHeaderAttestation
from opentimestamps.core.serialize import (
    BytesDeserializationContext,
    DeserializationError,
)
from opentimestamps.core.timestamp import DetachedTimestampFile

from ocfl_lib._errors import InvalidProofError, OtsConfigError, OtsParseError
from ocfl_lib._json_value import as_object, object_field_or_empty, parse_json
from ocfl_lib._path import join_path
from ocfl_lib.fs import _utils as fs_utils

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ocfl_lib.fs._protocols import FS

EXTENSION_NAME = "NNNN-ots-timestamps"
EXTENSION_DIR = f"extensions/{EXTENSION_NAME}"
CONFIG_PATH = f"{EXTENSION_DIR}/config.json"
PROOF_SUFFIX = ".ots"


def inventory_proof_path(version: str) -> str:
    """Object-relative path of the inventory proof of `version`."""
    return f"{EXTENSION_DIR}/inventory.json.{version}{PROOF_SUFFIX}"


@dataclass(frozen=True, slots=True, order=True)
class BitcoinAttestation:
    """A proof's claim that `commitment` is the merkle root of block `height`.

    `commitment` is in the block header's internal byte order.
    """

    height: int
    commitment: bytes


@dataclass(frozen=True, slots=True)
class ParsedOts:
    """A parsed proof.

    `file_hash` names the proof's file-hash operation (`sha256`, `sha1`, ...);
    `commitment_digest` is the file digest it commits to. `attestations` are
    its Bitcoin attestations, lowest first; `block_height` and
    `attested_digest` are those of the lowest one, or None for a pending
    proof. Attestations of other types are not kept.
    """

    commitment_digest: bytes
    attested_digest: bytes | None
    block_height: int | None
    file_hash: str = "sha256"
    attestations: tuple[BitcoinAttestation, ...] = ()

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

    attestations = tuple(
        sorted(
            {
                BitcoinAttestation(attestation.height, msg)
                for msg, attestation in detached.timestamp.all_attestations()
                if isinstance(attestation, BitcoinBlockHeaderAttestation)
            }
        )
    )
    lowest = attestations[0] if attestations else None
    return ParsedOts(
        commitment_digest=detached.timestamp.msg,
        attested_digest=lowest.commitment if lowest else None,
        block_height=lowest.height if lowest else None,
        file_hash=detached.file_hash_op.TAG_NAME,
        attestations=attestations,
    )


def verify_proof(raw: bytes, expected_hex: str) -> ParsedOts:
    """Parse a proof and check it commits to `expected_hex` with a sha256 file hash.

    Raises InvalidProofError (E905) for a proof that does not parse, uses
    another file-hash operation, or commits to another digest. A pending
    proof passes; check `is_final` for that.
    """
    try:
        parsed = parse(raw)
    except OtsParseError as e:
        raise InvalidProofError(str(e)) from e
    if parsed.file_hash != "sha256":
        raise InvalidProofError(f"file hash {parsed.file_hash}")
    if parsed.commitment_digest.hex() != expected_hex.lower():
        raise InvalidProofError(
            f"commits to {parsed.commitment_digest.hex()}, not {expected_hex}"
        )
    return parsed


@dataclass(frozen=True, slots=True)
class ChainBlock:
    """The block at a height on a node's best chain.

    `merkle_root` is in internal byte order, as attestations commit to it.
    """

    height: int
    block_hash: str
    merkle_root: bytes
    confirmations: int

    @classmethod
    def from_rpc(
        cls, *, height: int, block_hash: str, merkleroot: str, confirmations: int
    ) -> ChainBlock:
        """From bitcoind's header fields; its `merkleroot` hex is byte-reversed."""
        return cls(height, block_hash, bytes.fromhex(merkleroot)[::-1], confirmations)


@runtime_checkable
class BlockHeaderSource(Protocol):
    """Where block headers come from, such as a Bitcoin node."""

    def block_at(self, height: int) -> ChainBlock | None:
        """The block at `height` on the best chain, or None if there is none."""
        ...


class AttestationStatus(enum.StrEnum):
    VERIFIED = "verified"
    """The block has enough confirmations and its merkle root matches."""
    SHALLOW = "shallow"
    """No block at the height yet, or too few confirmations; not used."""
    MISMATCH = "mismatch"
    """The block has enough confirmations and another merkle root (E906)."""


@dataclass(frozen=True, slots=True)
class AttestationCheck:
    height: int
    commitment: bytes
    status: AttestationStatus
    block: ChainBlock | None


def check_attestations(
    proof: ParsedOts, source: BlockHeaderSource, *, confirmations: int
) -> tuple[AttestationCheck, ...]:
    """Check each Bitcoin attestation of `proof` against `source`.

    Pure apart from `source.block_at`. A pending proof has no checks. A
    proof passes when one check is VERIFIED and none is MISMATCH.
    """
    checks = []
    for att in proof.attestations:
        block = source.block_at(att.height)
        if block is None or block.confirmations < confirmations:
            status = AttestationStatus.SHALLOW
        elif block.merkle_root == att.commitment:
            status = AttestationStatus.VERIFIED
        else:
            status = AttestationStatus.MISMATCH
        checks.append(AttestationCheck(att.height, att.commitment, status, block))
    return tuple(checks)


@dataclass(frozen=True, slots=True)
class ChainCheck:
    """The client option that turns on Bitcoin checks in validation."""

    source: BlockHeaderSource
    confirmations: int


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


class StateSplit(NamedTuple):
    ordinary: frozenset[str]
    companion_of: dict[str, str]


def split_state(paths: Iterable[str]) -> StateSplit:
    """Split one version's state into ordinary paths and content proofs.

    `P.ots` is a content proof when `P` is in the same state. Every other
    path, including a standalone `x.ots`, is ordinary and needs a proof.
    """
    path_set = set(paths)
    proofs = {
        p: p.removesuffix(PROOF_SUFFIX)
        for p in path_set
        if p.endswith(PROOF_SUFFIX) and p.removesuffix(PROOF_SUFFIX) in path_set
    }
    return StateSplit(frozenset(path_set - proofs.keys()), proofs)


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


@dataclass(frozen=True, slots=True)
class ProofRef:
    """One proof of an object and the sha256 hex digest it must commit to.

    `path` is relative to the object root; `version` is the first covered
    version that holds the proof.
    """

    kind: Literal["content", "inventory"]
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
    versions = object_field_or_empty(inv, "versions")
    covered = covered_versions(cfg, versions)
    refs: list[ProofRef] = []
    if cfg.timestamp_content:
        first_path = {
            d: ps[0]
            for d, ps in object_field_or_empty(inv, "manifest").items()
            if isinstance(ps, list) and ps and isinstance(ps[0], str)
        }
        seen: set[str] = set()
        for v in covered:
            state = object_field_or_empty(as_object(versions.get(v)) or {}, "state")
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
            continue
        refs.append(ProofRef("inventory", v, path, hashlib.sha256(raw).hexdigest()))
    return tuple(refs)


@dataclass(frozen=True, slots=True)
class ProofChainResult:
    """The Bitcoin checks of one proof of an object."""

    proof: ProofRef
    checks: tuple[AttestationCheck, ...]

    @property
    def status(self) -> AttestationStatus | None:
        """MISMATCH if any check is; else VERIFIED, else SHALLOW; None when pending."""
        statuses = {c.status for c in self.checks}
        for status in (
            AttestationStatus.MISMATCH,
            AttestationStatus.VERIFIED,
            AttestationStatus.SHALLOW,
        ):
            if status in statuses:
                return status
        return None

    @property
    def verified(self) -> AttestationCheck | None:
        """The lowest verified attestation: the time of the proof."""
        return next(
            (c for c in self.checks if c.status is AttestationStatus.VERIFIED), None
        )


def check_object_chain(
    fsys: FS, obj_path: str, chain: ChainCheck
) -> tuple[ProofChainResult, ...]:
    """Bitcoin checks of every proof of the covered versions of an object.

    A proof that cannot be read or parsed is left out; the local checks
    report it (E905). Raises OtsConfigError for an invalid config; errors
    from `chain.source` propagate.
    """
    results = []
    for ref in object_proofs(fsys, obj_path):
        try:
            parsed = parse(fs_utils.read_all(fsys, join_path(obj_path, ref.path)))
        except OSError, OtsParseError:
            continue
        checks = check_attestations(
            parsed, chain.source, confirmations=chain.confirmations
        )
        results.append(ProofChainResult(ref, checks))
    return tuple(results)
