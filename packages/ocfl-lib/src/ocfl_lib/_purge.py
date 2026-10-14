"""Permanently remove an object (ADR-0001).

Purge refuses while a claim is live or stray. Then it deletes in this order:
the root inventory, the root sidecar, the version directories highest first,
`extensions/` and `logs/`, the NAMASTE, and last the empty object directory
and layout parents. Until the NAMASTE goes, what is left is still an object
(so never E072); after it, nothing but empty directories is left, which a
re-run of purge removes.
"""

import contextlib
from typing import TYPE_CHECKING

from ocfl_lib._commit import (
    object_lock,
    prune_parents,
    remove_if_exists,
)
from ocfl_lib._errors import ObjectNotFound, RecoveryRequiredError
from ocfl_lib._path import join_path
from ocfl_lib._recovery import CommitState, detect
from ocfl_lib._spec import INVENTORY_FILENAME, Namaste, VNum
from ocfl_lib.fs._protocols import ClaimFS

if TYPE_CHECKING:
    from ocfl_lib.fs._protocols import FsEntry, WriteFS


def purge(fsys: WriteFS, root_path: str, obj_path: str, object_id: str) -> None:
    """Remove the object at `obj_path`. Re-running finishes a partial purge."""
    with contextlib.ExitStack() as stack:
        if isinstance(fsys, ClaimFS):
            stack.enter_context(object_lock(fsys, root_path, object_id, timeout=0))
        status, plan = detect(fsys, root_path, obj_path, object_id)
        if plan is not None or status.state in (
            CommitState.STRAY,
            CommitState.UNREADABLE_PLAN,
        ):
            raise RecoveryRequiredError(
                f"{object_id} has an unfinished commit or stray claim "
                f"({status.state.value} {status.version}); run recover first"
            )
        entries = _entries(fsys, obj_path)
        if entries is None:
            if not _has_empty_parent(fsys, root_path, obj_path):
                raise ObjectNotFound(object_id)
        else:
            _delete_in_order(fsys, obj_path, entries)
        if isinstance(fsys, ClaimFS) and fsys.exists(obj_path):
            fsys.remove_empty_dir(obj_path)
        prune_parents(fsys, root_path, obj_path)


def _entries(fsys: WriteFS, obj_path: str) -> list[FsEntry] | None:
    try:
        return list(fsys.read_dir(obj_path))
    except FileNotFoundError, NotADirectoryError:
        return None


def _delete_in_order(fsys: WriteFS, obj_path: str, entries: list[FsEntry]) -> None:
    def path(name: str) -> str:
        return join_path(obj_path, name)

    names = {e.name for e in entries}
    if INVENTORY_FILENAME in names:
        remove_if_exists(fsys, path(INVENTORY_FILENAME))
    for name in sorted(n for n in names if n.startswith(f"{INVENTORY_FILENAME}.")):
        remove_if_exists(fsys, path(name))

    versions = []
    for e in entries:
        if e.is_dir:
            with contextlib.suppress(ValueError):
                versions.append((VNum.parse(e.name).number, e.name))
    for _n, name in sorted(versions, reverse=True):
        fsys.remove_all(path(name))

    for name in ("extensions", "logs"):
        if name in names:
            fsys.remove_all(path(name))

    done = {INVENTORY_FILENAME, "extensions", "logs"} | {n for _, n in versions}
    namastes = []
    for name in sorted(names - done):
        if name.startswith(f"{INVENTORY_FILENAME}."):
            continue
        n = Namaste.parse(name)
        if n is not None and n.kind == "object":
            namastes.append(name)
        else:
            fsys.remove_all(path(name))
    for name in namastes:
        remove_if_exists(fsys, path(name))


def _has_empty_parent(fsys: WriteFS, root_path: str, obj_path: str) -> bool:
    """A purge died after the object dir went but before its parents did."""
    if not isinstance(fsys, ClaimFS):
        return False
    p = obj_path.rpartition("/")[0]
    while p and p != root_path:
        if fsys.exists(p):
            return not any(True for _ in fsys.read_dir(p))
        p = p.rpartition("/")[0]
    return False
