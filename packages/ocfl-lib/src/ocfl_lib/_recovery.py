"""Find out how far a commit got, and finish or undo it (ADR-0001).

The storage root is the only record. `detect` reads the object and the work
dir and names the partial state; a recovery table per backend maps each
state and action to idempotent steps. Local and S3 have different partial
states (a local claim moves a complete `vN/`; an S3 claim is one key, and
the content follows it), so each has its own table.
"""

import enum
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import msgspec

from ocfl_lib import ots
from ocfl_lib._commit import (
    Target,
    check_base,
    discard,
    fill_claim,
    finish,
    finish_sidecar,
    is_s3,
    object_lock,
    prune_parents,
    read_or_none,
    remove_if_exists,
    revert_claimed,
    tidy_work_dir,
    work_dir,
    write_proof,
)
from ocfl_lib._commit import publish as publish_root
from ocfl_lib._commit_plan import CommitPlan
from ocfl_lib._digest import DigestAlgorithm
from ocfl_lib._errors import (
    PlanMismatchError,
    RecoveryActionError,
    RevertCompletedError,
    UnsupportedBackendError,
)
from ocfl_lib._inventory import Inventory, StoredInventory
from ocfl_lib._path import join_path
from ocfl_lib._spec import INVENTORY_FILENAME, VNum
from ocfl_lib.fs import _utils as fs_utils
from ocfl_lib.fs._protocols import ClaimFS, ConditionalFS

if TYPE_CHECKING:
    from ocfl_lib._sources import ContentSource
    from ocfl_lib.fs._protocols import FS, WriteFS


class CommitState(enum.Enum):
    """How far the last commit of an object got."""

    CLEAN = "clean"
    """No unfinished commit."""
    PLANNED = "planned"
    """The plan is written; nothing is in the object yet."""
    CLAIMED = "claimed"
    """`vN` is claimed but the root inventory is not replaced yet."""
    INVENTORY_PUBLISHED = "inventory_published"
    """The root inventory is replaced but the root sidecar is stale (E060)."""
    PUBLISHED = "published"
    """`vN` is published; only the plan is left."""
    STRAY = "stray"
    """`vN` is claimed and there is no plan: its writer is gone."""
    UNREADABLE_PLAN = "unreadable_plan"
    """`plan.json` exists but cannot be read, or is for another object.

    Resume or revert discards it; the object is then read on its own.
    """


class RecoveryAction(enum.Enum):
    RESUME = "resume"
    REVERT = "revert"
    REMOVE_STRAY = "remove_stray"


@dataclass(frozen=True, slots=True)
class CommitStatus:
    state: CommitState
    version: str | None = None


@dataclass(frozen=True)
class _Ctx:
    fsys: WriteFS
    root_path: str
    obj_path: str
    object_id: str
    status: CommitStatus
    plan: CommitPlan | None
    version: str | None
    content: ContentSource | None

    @property
    def target(self) -> Target:
        assert self.plan is not None
        return Target(self.fsys, self.root_path, self.obj_path, self.plan)

    def obj(self, *parts: str) -> str:
        return join_path(self.obj_path, *parts)


# --- detection


def detect(
    fsys: FS, root_path: str, obj_path: str, object_id: str
) -> tuple[CommitStatus, CommitPlan | None]:
    """Name the partial state of the object's last commit. Reads only."""
    wd = work_dir(root_path, object_id)
    plan_raw = read_or_none(fsys, join_path(wd, "plan.json"))
    plan = None
    if plan_raw is not None:
        try:
            plan = CommitPlan.from_json(plan_raw)
        except PlanMismatchError:
            return CommitStatus(CommitState.UNREADABLE_PLAN), None
        if plan.object_id != object_id:
            return CommitStatus(CommitState.UNREADABLE_PLAN), None
    root_raw = read_or_none(fsys, join_path(obj_path, INVENTORY_FILENAME))

    if plan is not None:
        v = plan.version
        if root_raw == plan.inventory.raw_bytes:
            if _sidecar_ok(fsys, obj_path, plan.inventory):
                return CommitStatus(CommitState.PUBLISHED, v), plan
            return CommitStatus(CommitState.INVENTORY_PUBLISHED, v), plan
        vinv = read_or_none(fsys, join_path(obj_path, v, INVENTORY_FILENAME))
        if vinv == plan.inventory.raw_bytes:
            return CommitStatus(CommitState.CLAIMED, v), plan
        return CommitStatus(CommitState.PLANNED, v), plan

    if root_raw is None:
        return CommitStatus(CommitState.CLEAN), None
    root = _parse_root(root_raw)
    if root is None:
        return CommitStatus(CommitState.CLEAN), None
    head = root.inventory.head
    head_raw = read_or_none(fsys, join_path(obj_path, head, INVENTORY_FILENAME))
    if not _sidecar_ok(fsys, obj_path, root) and head_raw == root_raw:
        return CommitStatus(CommitState.INVENTORY_PUBLISHED, head), None
    nxt = str(VNum.parse(head).next())
    if fsys.exists(join_path(obj_path, nxt)):
        return CommitStatus(CommitState.STRAY, nxt), None
    return CommitStatus(CommitState.CLEAN), None


def _parse_root(raw: bytes) -> StoredInventory | None:
    try:
        inv = msgspec.json.decode(raw, type=Inventory)
        alg = DigestAlgorithm.from_ocfl_name(inv.digest_algorithm)
    except msgspec.MsgspecError, ValueError:
        return None
    return StoredInventory.from_bytes(raw, alg)


def _sidecar_ok(fsys: FS, obj_path: str, inv: StoredInventory) -> bool:
    raw = read_or_none(fsys, join_path(obj_path, inv.sidecar_filename))
    if raw is None:
        return False
    parts = raw.decode(errors="replace").split()
    return bool(parts) and parts[0] == inv.digest


# --- recovery


def recover(
    fsys: WriteFS,
    root_path: str,
    obj_path: str,
    object_id: str,
    action: RecoveryAction,
    *,
    version: str | None = None,
    content: ContentSource | None = None,
) -> CommitStatus:
    """Finish or undo the object's last commit. Returns the state found.

    Every step is idempotent: recovering again, after a crash or not, picks
    up from what is on storage.
    """
    if isinstance(fsys, ClaimFS):
        with object_lock(fsys, work_dir(root_path, object_id), timeout=0):
            return _run(
                _LOCAL, fsys, root_path, obj_path, object_id, action, version, content
            )
    if isinstance(fsys, ConditionalFS):
        return _run(_S3, fsys, root_path, obj_path, object_id, action, version, content)
    raise UnsupportedBackendError(f"{type(fsys).__name__} cannot recover commits")


def _run(
    table: _Table,
    fsys: WriteFS,
    root_path: str,
    obj_path: str,
    object_id: str,
    action: RecoveryAction,
    version: str | None,
    content: ContentSource | None,
) -> CommitStatus:
    status, plan = detect(fsys, root_path, obj_path, object_id)
    ctx = _Ctx(fsys, root_path, obj_path, object_id, status, plan, version, content)
    table[status.state][action](ctx)
    _tidy(ctx)
    return status


# --- steps


def _tidy(ctx: _Ctx) -> None:
    """Drop work-dir leftovers that hold no plan (a probe, empty dirs)."""
    wd = work_dir(ctx.root_path, ctx.object_id)
    if not ctx.fsys.exists(join_path(wd, "plan.json")):
        if not is_s3(ctx.fsys) and ctx.fsys.exists(join_path(wd, "build")):
            ctx.fsys.remove_all(join_path(wd, "build"))
        remove_if_exists(ctx.fsys, join_path(wd, "probe"))
    tidy_work_dir(ctx.fsys, ctx.root_path, wd)
    prune_parents(ctx.fsys, ctx.root_path, ctx.obj_path)


def _nothing(ctx: _Ctx) -> None:
    pass


def _discard(ctx: _Ctx) -> None:
    discard(ctx.target)


def _base(ctx: _Ctx, raw: bytes | None) -> StoredInventory | None:
    """The base inventory, which a claimed version leaves at the root."""
    plan = ctx.target.plan
    if plan.base_digest is None or plan.base_digest_algorithm is None:
        return None
    alg = DigestAlgorithm.from_ocfl_name(plan.base_digest_algorithm)
    base = StoredInventory.from_bytes(raw, alg) if raw is not None else None
    if base is None or base.digest != plan.base_digest:
        check_base(ctx.fsys, ctx.target, raw)  # raises StaleHeadError
    return base


def _local_base(ctx: _Ctx) -> StoredInventory | None:
    return _base(ctx, read_or_none(ctx.fsys, ctx.obj(INVENTORY_FILENAME)))


def _s3_base(ctx: _Ctx) -> tuple[StoredInventory | None, str | None]:
    fsys = ctx.fsys
    assert isinstance(fsys, ConditionalFS)
    try:
        raw, tag = fsys.read_tagged(ctx.obj(INVENTORY_FILENAME))
    except FileNotFoundError:
        raw, tag = None, None
    return _base(ctx, raw), tag


def _resume_claimed_local(ctx: _Ctx) -> None:
    """The claimed `vN/` is complete: write the proof, then publish."""
    t = ctx.target
    base = _local_base(ctx)
    write_proof(t)
    publish_root(t, base)
    finish(t)


def _resume_claimed_s3(ctx: _Ctx) -> None:
    """Write what the claim lacks (content may need the source), then publish."""
    t = ctx.target
    base, tag = _s3_base(ctx)
    fill_claim(t, ctx.content or _NoContent())
    write_proof(t)
    publish_root(t, base, tag=tag)
    finish(t)


def _revert_claimed_local(ctx: _Ctx) -> None:
    revert_claimed(ctx.target, _local_base(ctx))


def _revert_claimed_s3(ctx: _Ctx) -> None:
    base, _tag = _s3_base(ctx)
    revert_claimed(ctx.target, base)


def _finish_sidecar(ctx: _Ctx) -> None:
    if ctx.plan is not None:
        plan = ctx.plan
        finish_sidecar(
            ctx.fsys, ctx.obj_path, plan.inventory, plan.base_digest_algorithm
        )
        finish(ctx.target)
        return
    raw = read_or_none(ctx.fsys, ctx.obj(INVENTORY_FILENAME))
    root = _parse_root(raw) if raw is not None else None
    if root is not None:
        fs_utils.write_bytes(
            ctx.fsys,
            ctx.obj(root.sidecar_filename),
            root.sidecar_content.encode(),
        )


def _drop_plan(ctx: _Ctx) -> None:
    """Discard a plan that cannot be read; the object is read on its own next."""
    remove_if_exists(
        ctx.fsys, join_path(work_dir(ctx.root_path, ctx.object_id), "plan.json")
    )


def _finish(ctx: _Ctx) -> None:
    finish(ctx.target)


def _published(ctx: _Ctx) -> None:
    raise RevertCompletedError(
        f"{ctx.object_id} {ctx.status.version} is already published; resume it instead"
    )


def _stray_version(ctx: _Ctx) -> str:
    v = ctx.status.version
    if v is None or ctx.version != v:
        raise RecoveryActionError(
            f"{ctx.object_id}: {ctx.version} is not a stray claim"
            + (f" (the stray claim is {v})" if v else "")
        )
    return v


def _remove_stray_local(ctx: _Ctx) -> None:
    """Remove the stray `vN/` and its inventory proof."""
    v = _stray_version(ctx)
    remove_if_exists(ctx.fsys, ctx.obj(ots.inventory_proof_path(v)))
    if ctx.fsys.exists(ctx.obj(v)):
        ctx.fsys.remove_all(ctx.obj(v))


def _remove_stray_s3(ctx: _Ctx) -> None:
    """Remove the stray proof and keys; the claim key `vN/inventory.json` last."""
    v = _stray_version(ctx)
    fsys = ctx.fsys
    assert isinstance(fsys, ConditionalFS)
    remove_if_exists(fsys, ctx.obj(ots.inventory_proof_path(v)))
    claim = ctx.obj(v, INVENTORY_FILENAME)
    for ref in list(fsys.walk_files(ctx.obj(v))):
        if ref.path != claim:
            remove_if_exists(fsys, ref.path)
    remove_if_exists(fsys, claim)


def _refuse(why: str) -> Callable[[_Ctx], None]:
    def step(ctx: _Ctx) -> None:
        detail = f" {ctx.status.version}" if ctx.status.version else ""
        raise RecoveryActionError(
            f"{ctx.object_id}: {ctx.status.state.value}{detail}: {why}"
        )

    return step


class _NoContent:
    def get_content(self, digest: str) -> tuple[FS, str]:
        from ocfl_lib._errors import ContentNotFound

        raise ContentNotFound(
            f"claimed version lacks content {digest}; pass its content source"
        )


# --- tables

type _Table = dict[CommitState, dict[RecoveryAction, Callable[[_Ctx], None]]]

_R, _V, _S = RecoveryAction.RESUME, RecoveryAction.REVERT, RecoveryAction.REMOVE_STRAY
_NOT_STRAY = _refuse("nothing to remove; there is no stray claim")
_NO_PLAN = _refuse("no plan to resume or revert; use remove-stray")
_DROP_FIRST = _refuse("discard the unreadable plan first with resume or revert")

_LOCAL: _Table = {
    CommitState.CLEAN: {_R: _nothing, _V: _nothing, _S: _NOT_STRAY},
    CommitState.PLANNED: {_R: _discard, _V: _discard, _S: _NOT_STRAY},
    CommitState.CLAIMED: {
        _R: _resume_claimed_local,
        _V: _revert_claimed_local,
        _S: _NOT_STRAY,
    },
    CommitState.INVENTORY_PUBLISHED: {
        _R: _finish_sidecar,
        _V: _published,
        _S: _NOT_STRAY,
    },
    CommitState.PUBLISHED: {_R: _finish, _V: _published, _S: _NOT_STRAY},
    CommitState.STRAY: {_R: _NO_PLAN, _V: _NO_PLAN, _S: _remove_stray_local},
    CommitState.UNREADABLE_PLAN: {_R: _drop_plan, _V: _drop_plan, _S: _DROP_FIRST},
}
"""Local: a claim moves a complete `vN/` (or a whole new object), so a claimed
version never lacks content and a new object is published by its claim."""

_S3: _Table = {
    CommitState.CLEAN: {_R: _nothing, _V: _nothing, _S: _NOT_STRAY},
    CommitState.PLANNED: {_R: _discard, _V: _discard, _S: _NOT_STRAY},
    CommitState.CLAIMED: {
        _R: _resume_claimed_s3,
        _V: _revert_claimed_s3,
        _S: _NOT_STRAY,
    },
    CommitState.INVENTORY_PUBLISHED: {
        _R: _finish_sidecar,
        _V: _published,
        _S: _NOT_STRAY,
    },
    CommitState.PUBLISHED: {_R: _finish, _V: _published, _S: _NOT_STRAY},
    CommitState.STRAY: {_R: _NO_PLAN, _V: _NO_PLAN, _S: _remove_stray_s3},
    CommitState.UNREADABLE_PLAN: {_R: _drop_plan, _V: _drop_plan, _S: _DROP_FIRST},
}
"""S3: the claim is the one key `vN/inventory.json`; content, the version
sidecar and the proof follow it, so resuming a claim may need the content
source. A revert deletes the claim key last."""

TABLES: dict[str, _Table] = {"local": _LOCAL, "s3": _S3}
