"""The commit plan: what one commit intends to write (ADR-0001).

A plan holds only intent: the object, the new version, the base and new
inventory digests, the new inventory bytes and an optional inventory proof.
It holds no progress. The storage root is the only record of how far a
commit got.
"""

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

import msgspec

from ocfl_lib import ots
from ocfl_lib._digest import DigestAlgorithm, bytes_digest, stream_digest
from ocfl_lib._errors import (
    OcflError,
    OtsParseError,
    PlanMismatchError,
    SpecVersionError,
)
from ocfl_lib._inventory import Inventory, StoredInventory, VersionInfo
from ocfl_lib._inventory_builder import InventoryBuilder
from ocfl_lib._path import join_path
from ocfl_lib._spec import Spec, VNum

if TYPE_CHECKING:
    from ocfl_lib._inventory import UserInfo
    from ocfl_lib._object import Object
    from ocfl_lib._version_state import VersionState
    from ocfl_lib.fs._protocols import FS

PLAN_FORMAT = 1


@dataclass(frozen=True, slots=True)
class CommitPlan:
    """The intent of one commit.

    `base_digest` and `base_digest_algorithm` identify the root inventory the
    plan was built on; both are None for a new object. `inventory_proof` is an
    OpenTimestamps proof for the bytes of `inventory`, written to the
    NNNN-ots-timestamps extension of the object.
    """

    object_id: str
    version: str
    base_digest: str | None
    base_digest_algorithm: str | None
    inventory: StoredInventory
    inventory_proof: bytes | None = None

    def __post_init__(self) -> None:
        inv = self.inventory.inventory
        if inv.id != self.object_id:
            raise PlanMismatchError(
                f"plan object {self.object_id!r} != inventory id {inv.id!r}"
            )
        if inv.head != self.version:
            raise PlanMismatchError(
                f"plan version {self.version} != inventory head {inv.head}"
            )
        if (self.base_digest is None) != (self.base_digest_algorithm is None):
            raise PlanMismatchError("plan base needs both digest and algorithm")
        is_first = VNum.parse(self.version).number == 1
        if is_first != (self.base_digest is None):
            raise PlanMismatchError(
                f"plan for {self.version} must "
                f"{'not ' if is_first else ''}have a base inventory"
            )
        if self.inventory_proof is not None:
            _check_proof(self.inventory, self.inventory_proof)

    @property
    def is_new_object(self) -> bool:
        return self.base_digest is None

    def to_json(self) -> bytes:
        """Encode as `plan.json` bytes."""
        base = None
        if self.base_digest is not None and self.base_digest_algorithm is not None:
            base = _Ref(self.base_digest_algorithm, self.base_digest)
        inv = self.inventory
        return msgspec.json.encode(
            _Wire(
                plan_format=PLAN_FORMAT,
                object_id=self.object_id,
                version=self.version,
                base=base,
                inventory=_Inv(
                    inv.digest_algorithm.ocfl_name, inv.digest, inv.raw_bytes
                ),
                inventory_proof=self.inventory_proof,
            )
        )

    @classmethod
    def from_json(cls, data: bytes) -> CommitPlan:
        """Decode `plan.json` bytes. Raises PlanMismatchError if invalid."""
        try:
            wire = msgspec.json.decode(data, type=_Wire)
        except msgspec.MsgspecError as exc:
            raise PlanMismatchError(f"invalid plan.json: {exc}") from exc
        if wire.plan_format != PLAN_FORMAT:
            raise PlanMismatchError(
                f"unsupported plan format {wire.plan_format}, expected {PLAN_FORMAT}"
            )
        try:
            alg = DigestAlgorithm.from_ocfl_name(wire.inventory.digest_algorithm)
            if bytes_digest(wire.inventory.raw, alg) != wire.inventory.digest:
                raise PlanMismatchError("plan inventory bytes do not match digest")
            base = wire.base
            return cls(
                object_id=wire.object_id,
                version=wire.version,
                base_digest=base.digest if base else None,
                base_digest_algorithm=base.digest_algorithm if base else None,
                inventory=StoredInventory.from_bytes(wire.inventory.raw, alg),
                inventory_proof=wire.inventory_proof,
            )
        except PlanMismatchError:
            raise
        except (OcflError, msgspec.MsgspecError) as exc:
            raise PlanMismatchError(f"invalid plan.json: {exc}") from exc


def _check_proof(inv: StoredInventory, proof: bytes) -> None:
    if inv.digest_algorithm is not DigestAlgorithm.SHA256:
        raise PlanMismatchError(
            "an inventory proof needs a sha256 inventory "
            f"(E901), got {inv.digest_algorithm.ocfl_name}"
        )
    try:
        parsed = ots.parse(proof)
    except OtsParseError as exc:
        raise PlanMismatchError(f"inventory proof does not parse: {exc}") from exc
    expected = hashlib.sha256(inv.raw_bytes).digest()
    if parsed.commitment_digest != expected:
        raise PlanMismatchError(
            f"inventory proof commits to {parsed.commitment_digest.hex()}, "
            f"not to the inventory digest {expected.hex()}"
        )


class _Ref(msgspec.Struct, rename="camel", forbid_unknown_fields=True):
    digest_algorithm: str
    digest: str


class _Inv(msgspec.Struct, rename="camel", forbid_unknown_fields=True):
    digest_algorithm: str
    digest: str
    raw: bytes = msgspec.field(name="bytes")


class _Wire(msgspec.Struct, rename="camel", forbid_unknown_fields=True):
    plan_format: int
    object_id: str
    version: str
    base: _Ref | None
    inventory: _Inv
    inventory_proof: bytes | None


def build_plan(
    obj: Object,
    version_state: VersionState,
    *,
    object_id: str,
    spec: Spec | None = None,
    message: str | None = None,
    user: UserInfo | None = None,
    created: str | None = None,
) -> CommitPlan:
    base = obj.inventory if obj.exists else None
    prev = base.inventory if base else None

    if prev is not None and spec is not None:
        prev_spec = Spec.from_inventory_type(prev.type)
        if prev_spec.is_newer_than(spec):
            raise SpecVersionError(
                f"E103: cannot downgrade object from spec {prev_spec} to {spec}"
            )

    if prev is not None:
        prev_alg = DigestAlgorithm.from_ocfl_name(prev.digest_algorithm)
        if prev_alg != version_state.digest_algorithm:
            prev = _migrate_algorithm(
                prev, obj.fs, obj.path, version_state.digest_algorithm
            )

    builder = InventoryBuilder(prev=prev)
    builder.digest_algorithm(version_state.digest_algorithm)
    if prev is None:
        builder.id(object_id)
    if spec is not None:
        builder.spec(spec)
    if version_state.fixity_source is not None:
        builder.fixity_source(version_state.fixity_source)
    builder.add_version(
        version_state.state, created=created, message=message, user=user
    )
    new_inv = builder.build()
    if new_inv.id != object_id:
        raise PlanMismatchError(
            f"object at {obj.path} has id {new_inv.id!r}, not {object_id!r}"
        )

    alg = DigestAlgorithm.from_ocfl_name(new_inv.digest_algorithm)
    return CommitPlan(
        object_id=object_id,
        version=new_inv.head,
        base_digest=base.digest if base else None,
        base_digest_algorithm=base.digest_algorithm.ocfl_name if base else None,
        inventory=StoredInventory.from_inventory(new_inv, alg),
    )


def _migrate_algorithm(
    prev: Inventory,
    fsys: FS,
    obj_path: str,
    new_alg: DigestAlgorithm,
) -> Inventory:
    old_alg = DigestAlgorithm.from_ocfl_name(prev.digest_algorithm)

    old_to_new: dict[str, str] = {}
    for old_digest, content_paths in prev.manifest.items():
        full_path = join_path(obj_path, content_paths[0])
        with fsys.open_file(full_path) as f:
            old_to_new[old_digest] = stream_digest(f, new_alg)

    new_manifest: dict[str, list[str]] = {}
    for old_digest, content_paths in prev.manifest.items():
        new_manifest[old_to_new[old_digest]] = list(content_paths)
    new_manifest = dict(sorted(new_manifest.items()))

    new_versions: dict[str, VersionInfo] = {}
    for vkey, vinfo in prev.versions.items():
        new_state: dict[str, list[str]] = {}
        for old_digest, logical_paths in vinfo.state.items():
            nd = old_to_new[old_digest]
            if nd in new_state:
                new_state[nd].extend(logical_paths)
            else:
                new_state[nd] = list(logical_paths)
        new_versions[vkey] = VersionInfo(
            created=vinfo.created,
            state=dict(sorted(new_state.items())),
            message=vinfo.message,
            user=vinfo.user,
        )

    fixity: dict[str, dict[str, list[str]]] = {}
    if prev.fixity:
        for alg_name, dm in prev.fixity.items():
            fixity[alg_name] = {d: list(ps) for d, ps in dm.items()}
    fixity[old_alg.ocfl_name] = {d: list(ps) for d, ps in prev.manifest.items()}

    return Inventory(
        id=prev.id,
        type=prev.type,
        digest_algorithm=new_alg.ocfl_name,
        head=prev.head,
        manifest=new_manifest,
        versions=new_versions,
        content_directory=prev.content_directory,
        fixity=fixity or None,
    )
