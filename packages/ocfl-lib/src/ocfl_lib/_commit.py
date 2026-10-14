"""Write one version of an object: claim, then publish (ADR-0001).

Local (Linux): lock the object, check the base, write the plan, build the
complete `vN/` (or the whole new object) in the work dir, claim it with
renameat2(RENAME_NOREPLACE), write the inventory proof, then publish the
root inventory and, last, the root sidecar.

S3: check the base, write the plan create-only, claim with a create-only
`vN/inventory.json`, write the content and version sidecar, then the
inventory proof, then publish the root inventory with If-Match on the base
ETag and, last, the root sidecar. A new object writes its NAMASTE right
after the claim.

The plan holds only intent. How far a commit got is read back from storage.
"""

import contextlib
import errno
import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, BinaryIO

from ocfl_lib import ots
from ocfl_lib._concurrent import bounded_map
from ocfl_lib._digest import DigestAlgorithm, bytes_digest, stream_digest
from ocfl_lib._errors import (
    ClaimLostError,
    ContentDigestMismatch,
    InvalidProofError,
    MissingContentProofError,
    MissingInventoryProofError,
    ObjectLockedError,
    OtsConfigError,
    PendingProofError,
    PlanMismatchError,
    RecoveryRequiredError,
    RevertCompletedError,
    StaleHeadError,
    UnsupportedBackendError,
)
from ocfl_lib._inventory import StoredInventory
from ocfl_lib._path import join_path
from ocfl_lib._spec import INVENTORY_FILENAME, Namaste, Spec
from ocfl_lib.fs import _utils as fs_utils
from ocfl_lib.fs._protocols import ClaimFS, ConditionalFS, CopyFS

if TYPE_CHECKING:
    from collections.abc import Generator

    from ocfl_lib._commit_plan import CommitPlan
    from ocfl_lib._sources import ContentSource
    from ocfl_lib.fs._protocols import FS, WriteFS

WORK_DIR_EXTENSION = "NNNN-work-dir"
LOCK_TIMEOUT = 5.0

_NO_RENAMEAT2 = {errno.EINVAL, errno.ENOSYS, errno.EXDEV, errno.EOPNOTSUPP}


def work_dir(root_path: str, object_id: str) -> str:
    """The object's directory in the storage root's work-dir extension."""
    key = hashlib.sha256(object_id.encode()).hexdigest()
    return join_path(root_path, "extensions", WORK_DIR_EXTENSION, key)


def is_s3(fsys: WriteFS) -> bool:
    """True for a store that claims by conditional write, not by rename."""
    return isinstance(fsys, ConditionalFS) and not isinstance(fsys, ClaimFS)


@dataclass(frozen=True)
class Target:
    """Where one commit writes."""

    fsys: WriteFS
    root_path: str
    obj_path: str
    plan: CommitPlan

    def obj(self, *parts: str) -> str:
        return join_path(self.obj_path, *parts)

    @property
    def vn(self) -> str:
        return self.plan.version

    @property
    def wd(self) -> str:
        return work_dir(self.root_path, self.plan.object_id)

    @property
    def plan_file(self) -> str:
        return join_path(self.wd, "plan.json")

    @property
    def build(self) -> str:
        return join_path(self.wd, "build")

    @property
    def claim(self) -> str:
        """The local claim (`vN/` or the new object) or the S3 claim key."""
        if is_s3(self.fsys):
            return self.obj(self.vn, INVENTORY_FILENAME)
        return self.obj_path if self.plan.is_new_object else self.obj(self.vn)


def commit(
    fsys: WriteFS,
    root_path: str,
    obj_path: str,
    plan: CommitPlan,
    content: ContentSource,
) -> None:
    """Write `plan` into the object at `obj_path`.

    On an Exception or KeyboardInterrupt the commit undoes its own writes,
    unless the root inventory is already published. A process that dies
    leaves the partial state for `recover`.
    """
    t = Target(fsys, root_path, obj_path, plan)
    if isinstance(fsys, ClaimFS):
        _commit_local(t, fsys, content)
    elif isinstance(fsys, ConditionalFS):
        _commit_s3(t, fsys, content)
    else:
        raise UnsupportedBackendError(
            f"{type(fsys).__name__} can neither rename create-only nor write "
            "conditionally, so it cannot claim a version"
        )


# --- local


def _commit_local(t: Target, fsys: ClaimFS, content: ContentSource) -> None:
    with object_lock(fsys, t.wd, timeout=LOCK_TIMEOUT):
        if fsys.exists(t.plan_file):
            raise RecoveryRequiredError(_unfinished(t))
        try:
            raw = read_or_none(fsys, t.obj(INVENTORY_FILENAME))
            base = check_base(fsys, t, raw, content)
        except Exception:
            tidy_work_dir(fsys, t.root_path, t.wd)  # the lock made it (W016)
            raise
        fs_utils.write_bytes(fsys, t.plan_file, t.plan.to_json())
        claimed = False
        try:
            _build(t, fsys, content)
            _claim_local(t, fsys)
            claimed = True
            if base is not None:
                write_proof(t)
                publish(t, base)
            finish(t)
        except (Exception, KeyboardInterrupt) as exc:
            _undo(t, base, claimed=claimed, exc=exc)
            raise


@contextlib.contextmanager
def object_lock(fsys: ClaimFS, wd: str, *, timeout: float) -> Generator[None]:
    """Hold the object's commit lock. ObjectLockedError if it is busy."""
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(fsys.lock(wd, timeout=timeout))
        except BlockingIOError as e:
            raise ObjectLockedError(
                f"another process is writing this object ({wd})"
            ) from e
        yield


def _build(t: Target, fsys: ClaimFS, content: ContentSource) -> None:
    """Build the complete `vN/`, or the whole new object, in the work dir."""
    plan = t.plan
    if fsys.exists(t.build):
        fsys.remove_all(t.build)

    def dest(rel: str) -> str:
        if plan.is_new_object:
            return join_path(t.build, rel)
        return join_path(t.build, rel.removeprefix(f"{t.vn}/"))

    items = [(d, dest(p)) for d, p in content_items(plan)]
    copy_content(fsys, content, items, plan.inventory.digest_algorithm)
    files = version_files(plan)
    if plan.is_new_object:
        files |= new_object_files(plan) | root_files(plan)
    for rel, data in files.items():
        fs_utils.write_bytes(fsys, dest(rel), data)


def _claim_local(t: Target, fsys: ClaimFS) -> None:
    """Rename the build into the object; the loser has written nothing there."""
    for _ in range(3):
        if t.plan.is_new_object:
            parent = t.obj_path.rpartition("/")[0]
            if parent and parent != t.root_path:
                fsys.mkdir(parent)
        try:
            fsys.rename_noreplace(t.build, t.claim)
            return
        except FileExistsError as e:
            raise ClaimLostError(
                f"{t.plan.object_id} {t.vn} was claimed by another writer"
            ) from e
        except FileNotFoundError:
            if not t.plan.is_new_object:
                raise
            # a concurrent prune removed the parent; make it again
        except OSError as e:
            if e.errno in _NO_RENAMEAT2:
                raise UnsupportedBackendError(
                    f"cannot claim {t.claim}: renameat2(RENAME_NOREPLACE) "
                    f"failed ({e.strerror})"
                ) from e
            raise
    raise ClaimLostError(f"cannot claim {t.claim}: its parent keeps disappearing")


# --- S3


def _commit_s3(t: Target, fsys: ConditionalFS, content: ContentSource) -> None:
    if fsys.exists(t.plan_file):
        raise RecoveryRequiredError(_unfinished(t))
    try:
        raw, tag = fsys.read_tagged(t.obj(INVENTORY_FILENAME))
    except FileNotFoundError:
        raw, tag = None, None
    base = check_base(fsys, t, raw, content)
    fsys.check_conditional_writes(join_path(t.wd, "probe"))
    try:
        fsys.write_new(t.plan_file, t.plan.to_json())
    except FileExistsError as e:
        raise RecoveryRequiredError(_unfinished(t)) from e
    claimed = False
    try:
        try:
            fsys.write_new(t.claim, t.plan.inventory.raw_bytes)
        except FileExistsError as e:
            raise ClaimLostError(
                f"{t.plan.object_id} {t.vn} was claimed by another writer"
            ) from e
        claimed = True
        fill_claim(t, content)
        write_proof(t)
        publish(t, base, tag=tag)
        finish(t)
    except (Exception, KeyboardInterrupt) as exc:
        _undo(t, base, claimed=claimed, exc=exc)
        raise


def fill_claim(t: Target, content: ContentSource) -> None:
    """S3: write what a claimed version still lacks (idempotent)."""
    fsys = t.fsys
    plan = t.plan
    if plan.is_new_object:
        _write_missing(t, new_object_files(plan))
    missing = [
        (d, t.obj(p)) for d, p in content_items(plan) if not fsys.exists(t.obj(p))
    ]
    copy_content(fsys, content, missing, plan.inventory.digest_algorithm)
    _write_missing(t, version_files(plan))


def _write_missing(t: Target, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        if rel == ots.inventory_proof_path(t.vn):
            continue  # written by write_proof, after the content
        path = t.obj(rel)
        if read_or_none(t.fsys, path) != data:
            fs_utils.write_bytes(t.fsys, path, data)


# --- shared steps


def check_base(
    fsys: FS, t: Target, raw: bytes | None, content: ContentSource | None = None
) -> StoredInventory | None:
    """Check the object is still at the plan's base. Return the base inventory.

    Refuses a stray claim (RecoveryRequiredError), an inventory proof the
    object cannot hold, and a version its OTS extension would reject. With
    `content`, the content proofs of the version are checked too.
    """
    plan = t.plan
    if plan.base_digest is None or plan.base_digest_algorithm is None:
        if raw is not None:
            raise StaleHeadError(
                object_path=t.obj_path,
                expected_digest=None,
                actual_digest=bytes_digest(raw, plan.inventory.digest_algorithm),
            )
        if fsys.exists(t.obj_path):
            raise RecoveryRequiredError(
                f"{t.obj_path} holds files but no object; run recover"
            )
        if plan.inventory_proof is not None:
            check_extension(plan, start_config(plan))
            _check_content_proofs(t, start_config(plan), content)
        return None

    alg = DigestAlgorithm.from_ocfl_name(plan.base_digest_algorithm)
    actual = bytes_digest(raw, alg) if raw is not None else None
    if raw is None or actual != plan.base_digest:
        raise StaleHeadError(
            object_path=t.obj_path,
            expected_digest=plan.base_digest,
            actual_digest=actual,
        )
    if fsys.exists(t.obj(t.vn)):
        raise RecoveryRequiredError(
            f"{t.obj_path} has a stray claim on {t.vn}; run recover"
        )
    base = StoredInventory.from_bytes(raw, alg)
    cfg = ots.read_config(fsys, t.obj_path)
    if cfg is None and plan.inventory_proof is not None:
        _check_all_sha256(fsys, t, base)
        cfg = start_config(plan)
    if cfg is not None:
        check_extension(plan, cfg)
        _check_content_proofs(t, cfg, content)
    return base


def start_config(plan: CommitPlan) -> ots.OtsConfig:
    """The config a proof starts: from v1 for a new object, else from vN."""
    return ots.OtsConfig(timestamp_from=None if plan.is_new_object else plan.version)


def _check_all_sha256(fsys: FS, t: Target, base: StoredInventory) -> None:
    """E901 covers every inventory, also those before `timestampFrom`."""
    algs = {base.digest_algorithm.ocfl_name}
    for v in base.inventory.versions:
        raw = read_or_none(fsys, t.obj(v, INVENTORY_FILENAME))
        if raw is not None:
            try:
                algs.add(json.loads(raw).get("digestAlgorithm"))
            except ValueError, AttributeError:
                algs.add(None)
    if algs != {"sha256"}:
        raise PlanMismatchError(
            f"object {t.plan.object_id!r} cannot start the {ots.EXTENSION_NAME} "
            "extension: every inventory must use sha256 (E901)"
        )


def check_extension(plan: CommitPlan, cfg: ots.OtsConfig) -> None:
    """Refuse a version that the OTS extension rejects by its inventory alone.

    The commit would otherwise publish an object that is invalid: E901 (not
    sha256), E904 (no inventory proof) or E902 (a path without its proof).
    """
    alg = plan.inventory.digest_algorithm.ocfl_name
    if alg != "sha256" or cfg.proof_digest_algorithm != "sha256":
        raise PlanMismatchError(
            f"the {ots.EXTENSION_NAME} extension needs sha256, not {alg} (E901)"
        )
    if cfg.timestamp_inventory and plan.inventory_proof is None:
        raise MissingInventoryProofError(
            f"object {plan.object_id!r} requires an inventory proof for "
            f"{plan.version} (E904)"
        )
    if cfg.timestamp_content:
        state = plan.inventory.inventory.versions[plan.version].state
        ordinary, proofs = ots.split_state(p for ps in state.values() for p in ps)
        missing = sorted(p for p in ordinary if p + ots.PROOF_SUFFIX not in proofs)
        if missing:
            raise MissingContentProofError(
                f"{plan.version} has no content proof for {', '.join(missing)} (E902)"
            )


def _check_content_proofs(
    t: Target, cfg: ots.OtsConfig, content: ContentSource | None
) -> None:
    """Refuse a content proof that is invalid (E905) or pending (E907).

    A proof must be final before its version is committed. New proofs are
    read from `content`, carried-forward ones from the object.
    """
    if content is None or not cfg.timestamp_content:
        return
    plan = t.plan
    inv = plan.inventory.inventory
    digest_of = {p: d for d, ps in inv.versions[plan.version].state.items() for p in ps}
    _ordinary, proofs = ots.split_state(digest_of)
    seen: set[tuple[str, str]] = set()
    for proof, companion in sorted(proofs.items()):
        key = (digest_of[proof], digest_of[companion])
        if key in seen:
            continue
        seen.add(key)
        raw = _content_bytes(t, content, key[0])
        try:
            parsed = ots.verify_proof(raw, key[1])
        except InvalidProofError as e:
            raise InvalidProofError(
                f"{plan.version} content proof {proof}: {e} (E905)"
            ) from e
        if not parsed.is_final:
            raise PendingProofError(
                f"{plan.version} content proof {proof} is pending (E907)"
            )


def _content_bytes(t: Target, content: ContentSource, digest: str) -> bytes:
    """The bytes of `digest`: new in vN from `content`, else from the object."""
    path = t.plan.inventory.inventory.manifest[digest][0]
    if path.startswith(f"{t.vn}/"):
        src_fs, src_path = content.get_content(digest)
        raw = fs_utils.read_all(src_fs, src_path)
    else:
        raw = fs_utils.read_all(t.fsys, t.obj(path))
    actual = bytes_digest(raw, t.plan.inventory.digest_algorithm)
    if actual != digest.lower():
        raise ContentDigestMismatch(f"content for {digest} has digest {actual}")
    return raw


def write_proof(t: Target) -> None:
    """Write the plan's inventory proof, or remove a stale one for vN.

    A proof for an existing object without the extension starts it: the
    config, with `timestampFrom` set to vN, is written just before the proof.
    """
    path = t.obj(ots.inventory_proof_path(t.vn))
    if t.plan.inventory_proof is not None:
        config = t.obj(ots.CONFIG_PATH)
        if not t.plan.is_new_object and read_or_none(t.fsys, config) is None:
            fs_utils.write_bytes(t.fsys, config, start_config(t.plan).to_json())
        if read_or_none(t.fsys, path) != t.plan.inventory_proof:
            fs_utils.write_bytes(t.fsys, path, t.plan.inventory_proof)
    else:
        remove_if_exists(t.fsys, path)


def publish(t: Target, base: StoredInventory | None, *, tag: str | None = None) -> None:
    """Replace the root inventory, then write the root sidecar last.

    A spec upgrade swaps the NAMASTE just before. On S3 the root inventory
    is written with If-Match on the base ETag (`tag`), or create-only for
    a new object.
    """
    fsys = t.fsys
    plan = t.plan
    swap = namaste_swap(base, plan)
    if swap is not None:
        old, new = swap
        fs_utils.write_bytes(fsys, t.obj(new.filename), new.content.encode())
        remove_if_exists(fsys, t.obj(old.filename))

    rinv = t.obj(INVENTORY_FILENAME)
    if isinstance(fsys, ConditionalFS) and is_s3(fsys):
        if base is None:
            try:
                fsys.write_new(rinv, plan.inventory.raw_bytes)
            except FileExistsError as e:
                raise StaleHeadError(
                    object_path=t.obj_path,
                    expected_digest=None,
                    actual_digest=plan.inventory.digest,
                ) from e
        elif (
            tag is None
            or fsys.write_if_match(rinv, plan.inventory.raw_bytes, tag) is None
        ):
            raise StaleHeadError(
                object_path=t.obj_path,
                expected_digest=plan.base_digest,
                actual_digest=None,
            )
    else:
        _check_still_base(t, base)
        fs_utils.write_bytes(fsys, rinv, plan.inventory.raw_bytes)
    finish_sidecar(fsys, t.obj_path, plan.inventory, plan.base_digest_algorithm)


def finish_sidecar(
    fsys: WriteFS,
    obj_path: str,
    inv: StoredInventory,
    old_algorithm: str | None,
) -> None:
    """Make the root sidecar certify `inv`; an old-algorithm one goes first."""
    if old_algorithm and old_algorithm != inv.digest_algorithm.ocfl_name:
        remove_if_exists(fsys, join_path(obj_path, f"inventory.json.{old_algorithm}"))
    fs_utils.write_bytes(
        fsys,
        join_path(obj_path, inv.sidecar_filename),
        inv.sidecar_content.encode(),
    )


def _check_still_base(t: Target, base: StoredInventory | None) -> None:
    raw = read_or_none(t.fsys, t.obj(INVENTORY_FILENAME))
    if base is None or raw != base.raw_bytes:
        alg = t.plan.inventory.digest_algorithm
        raise StaleHeadError(
            object_path=t.obj_path,
            expected_digest=base.digest if base else None,
            actual_digest=None if raw is None else bytes_digest(raw, alg),
        )


def finish(t: Target) -> None:
    """The version is published: drop the plan and the work dir."""
    remove_if_exists(t.fsys, t.plan_file)
    tidy_work_dir(t.fsys, t.root_path, t.wd)


def discard(t: Target) -> None:
    """Undo an unclaimed commit: nothing of it is in the object."""
    if not is_s3(t.fsys) and t.fsys.exists(t.build):
        t.fsys.remove_all(t.build)
    remove_if_exists(t.fsys, join_path(t.wd, "probe"))
    remove_if_exists(t.fsys, t.plan_file)
    tidy_work_dir(t.fsys, t.root_path, t.wd)
    prune_parents(t.fsys, t.root_path, t.obj_path)


def revert_claimed(t: Target, base: StoredInventory | None) -> None:
    """Remove a claimed, unpublished version. The plan goes last."""
    fsys = t.fsys
    if read_or_none(fsys, t.obj(INVENTORY_FILENAME)) == t.plan.inventory.raw_bytes:
        raise RevertCompletedError(
            f"{t.plan.object_id} {t.vn} is already published; resume it instead"
        )
    swap = namaste_swap(base, t.plan)
    if swap is not None:
        old, new = swap
        if not fsys.exists(t.obj(old.filename)):
            fs_utils.write_bytes(fsys, t.obj(old.filename), old.content.encode())
        remove_if_exists(fsys, t.obj(new.filename))
    remove_if_exists(fsys, t.obj(ots.inventory_proof_path(t.vn)))
    unstart_extension(fsys, t.obj_path, t.vn)
    if isinstance(fsys, ConditionalFS) and is_s3(fsys):
        prefix = t.obj_path if t.plan.is_new_object else t.obj(t.vn)
        for ref in list(fsys.walk_files(prefix)):
            if ref.path != t.claim:
                remove_if_exists(fsys, ref.path)
        remove_if_exists(fsys, t.claim)
    elif fsys.exists(t.claim):
        fsys.remove_all(t.claim)
    discard(t)


def unstart_extension(fsys: WriteFS, obj_path: str, version: str) -> None:
    """Remove the extension that unpublished `version` started, if it did.

    Only a config whose `timestampFrom` is `version` was written by the
    commit of `version`; its proof goes first, then the config, then the
    empty directories.
    """
    raw = read_or_none(fsys, join_path(obj_path, ots.CONFIG_PATH))
    if raw is None:
        return
    try:
        cfg = ots.OtsConfig.from_json(raw)
    except OtsConfigError:
        return
    if cfg.timestamp_from != version:
        return
    remove_if_exists(fsys, join_path(obj_path, ots.inventory_proof_path(version)))
    remove_if_exists(fsys, join_path(obj_path, ots.CONFIG_PATH))
    if isinstance(fsys, ClaimFS):
        for d in (ots.EXTENSION_DIR, "extensions"):
            if not fsys.remove_empty_dir(join_path(obj_path, d)):
                return


def _undo(
    t: Target,
    base: StoredInventory | None,
    *,
    claimed: bool,
    exc: BaseException,
) -> None:
    try:
        if claimed:
            revert_claimed(t, base)
        else:
            discard(t)
    except RevertCompletedError:
        exc.add_note(f"{t.vn} is published; run recover --resume to finish it")
    except Exception as undo_exc:
        exc.add_note(f"undo failed ({undo_exc}); run recover on the object")


def tidy_work_dir(fsys: WriteFS, root_path: str, wd: str) -> None:
    """Remove the object's work dir and the work-dir extension if empty."""
    if not isinstance(fsys, ClaimFS):
        return
    ext = join_path(root_path, "extensions")
    for d in (wd, join_path(ext, WORK_DIR_EXTENSION), ext):
        if not fsys.remove_empty_dir(d):
            return


def prune_parents(fsys: WriteFS, root_path: str, obj_path: str) -> None:
    """Remove the empty layout parents of `obj_path` (E073).

    The object dir itself is left alone: a commit never makes it before its
    claim, so an object dir there belongs to someone else.
    """
    if not isinstance(fsys, ClaimFS):
        return
    path = obj_path.rpartition("/")[0]
    while path and path != root_path:
        if fsys.exists(path) and not fsys.remove_empty_dir(path):
            return
        path = path.rpartition("/")[0]


# --- version payload


def content_items(plan: CommitPlan) -> list[tuple[str, str]]:
    """(digest, content path) of every content file new in this version."""
    prefix = f"{plan.version}/"
    return [
        (digest, path)
        for digest, paths in plan.inventory.inventory.manifest.items()
        for path in paths
        if path.startswith(prefix)
    ]


def version_files(plan: CommitPlan) -> dict[str, bytes]:
    inv = plan.inventory
    return {
        f"{plan.version}/{INVENTORY_FILENAME}": inv.raw_bytes,
        f"{plan.version}/{inv.sidecar_filename}": inv.sidecar_content.encode(),
    }


def new_object_files(plan: CommitPlan) -> dict[str, bytes]:
    spec = Spec.from_inventory_type(plan.inventory.inventory.type)
    namaste = Namaste.for_object(spec)
    files = {namaste.filename: namaste.content.encode()}
    if plan.inventory_proof is not None:
        files[ots.CONFIG_PATH] = start_config(plan).to_json()
        files[ots.inventory_proof_path(plan.version)] = plan.inventory_proof
    return files


def root_files(plan: CommitPlan) -> dict[str, bytes]:
    inv = plan.inventory
    return {
        INVENTORY_FILENAME: inv.raw_bytes,
        inv.sidecar_filename: inv.sidecar_content.encode(),
    }


def namaste_swap(
    base: StoredInventory | None, plan: CommitPlan
) -> tuple[Namaste, Namaste] | None:
    """(old, new) object declarations if the plan upgrades the spec."""
    if base is None:
        return None
    old = Spec.from_inventory_type(base.inventory.type)
    new = Spec.from_inventory_type(plan.inventory.inventory.type)
    if old == new:
        return None
    return Namaste.for_object(old), Namaste.for_object(new)


def copy_content(
    fsys: WriteFS,
    content: ContentSource,
    items: list[tuple[str, str]],
    alg: DigestAlgorithm,
) -> None:
    """Copy each (digest, dst) from `content`, checking the bytes on the way.

    A copy whose bytes do not match its manifest digest is removed and
    raises ContentDigestMismatch, before anything is published.
    """

    def _copy(item: tuple[str, str]) -> None:
        digest, dst = item
        parent = dst.rpartition("/")[0]
        if parent:
            fsys.mkdir(parent)
        src_fs, src_path = content.get_content(digest)
        if src_fs is fsys and isinstance(fsys, CopyFS):
            fsys.copy(src_path, dst)  # server-side; check what landed
            with fsys.open_file(dst) as f:
                actual = stream_digest(f, alg)
        else:
            with src_fs.open_file(src_path) as f:
                reader = _HashingReader(f, alg)
                fsys.write(dst, reader)  # ty: ignore[invalid-argument-type]
            actual = reader.hexdigest()
        if actual.lower() != digest.lower():
            remove_if_exists(fsys, dst)
            raise ContentDigestMismatch(
                f"content for {digest} from {src_path} has digest {actual}"
            )

    for _ in bounded_map(_copy, items):
        pass


class _HashingReader:
    """Digests a stream as it is read."""

    def __init__(self, f: BinaryIO, alg: DigestAlgorithm) -> None:
        self._f = f
        self._h = alg.new_hasher()

    def read(self, size: int = -1) -> bytes:
        chunk = self._f.read(size)
        self._h.update(chunk)
        return chunk

    def hexdigest(self) -> str:
        return self._h.hexdigest()


# --- small helpers


def read_or_none(fsys: FS, path: str) -> bytes | None:
    try:
        return fs_utils.read_all(fsys, path)
    except FileNotFoundError:
        return None


def remove_if_exists(fsys: WriteFS, path: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        fsys.remove(path)


def _unfinished(t: Target) -> str:
    return f"{t.plan.object_id} has an unfinished commit; run recover first"
