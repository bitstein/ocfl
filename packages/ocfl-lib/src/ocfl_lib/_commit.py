"""Write one version of an object from a CommitPlan (ADR-0001)."""

import json
from typing import TYPE_CHECKING

from ocfl_lib._digest import DigestAlgorithm, bytes_digest
from ocfl_lib._errors import PlanMismatchError, RevertError, StaleHeadError
from ocfl_lib._object import Object
from ocfl_lib._path import join_path
from ocfl_lib._spec import INVENTORY_FILENAME
from ocfl_lib._update_plan import (
    UpdatePlan,
    execute_plan,
    prepare_steps,
    revert_plan,
)
from ocfl_lib.fs import _utils as fs_utils
from ocfl_lib.validation._extension_ots import EXTENSION_NAME as OTS_EXTENSION

if TYPE_CHECKING:
    from ocfl_lib._commit_plan import CommitPlan
    from ocfl_lib._inventory import StoredInventory
    from ocfl_lib._sources import ContentSource
    from ocfl_lib.fs._protocols import FS, WriteFS

OTS_DIR = f"extensions/{OTS_EXTENSION}"
OTS_CONFIG_PATH = f"{OTS_DIR}/config.json"
OTS_CONFIG_BYTES = json.dumps(
    {
        "extensionName": OTS_EXTENSION,
        "timestampContent": True,
        "timestampInventory": True,
    },
    sort_keys=True,
    separators=(",", ":"),
).encode()


def proof_path(version: str) -> str:
    """Object-relative path of the inventory proof of `version`."""
    return f"{OTS_DIR}/inventory.json.{version}.ots"


def commit(
    fsys: WriteFS,
    obj: Object,
    plan: CommitPlan,
    content: ContentSource,
) -> Object:
    """Write `plan` into `obj`. Reverts on failure, then re-raises."""
    up = executor_plan(obj, plan)
    prepare_steps(up, fsys, obj.path, content)
    try:
        execute_plan(up)
    except (Exception, KeyboardInterrupt) as exc:
        try:
            revert_plan(up)
        except RevertError as rev_exc:
            raise rev_exc from exc
        raise
    return Object(obj.fs, obj.path)


def executor_plan(obj: Object, plan: CommitPlan) -> UpdatePlan:
    """Check the base and expand `plan` into in-memory update steps."""
    base = check_base(obj.fs, obj.path, plan)
    return UpdatePlan(
        steps=[],
        new_inv=plan.inventory,
        old_inv=base,
        old_sidecar_alg=_old_sidecar_alg(base, plan),
        extension_files=_extension_files(plan),
    )


def check_base(fsys: FS, obj_path: str, plan: CommitPlan) -> StoredInventory | None:
    """Check the object is still at the plan's base. Return the base inventory.

    Also refuses an inventory proof the object cannot hold: an existing object
    must already use the OTS extension.
    """
    from ocfl_lib._inventory import StoredInventory

    try:
        raw = fs_utils.read_all(fsys, join_path(obj_path, INVENTORY_FILENAME))
    except FileNotFoundError:
        raw = None

    if plan.base_digest is None or plan.base_digest_algorithm is None:
        if raw is not None:
            actual = bytes_digest(raw, plan.inventory.digest_algorithm)
            raise StaleHeadError(
                object_path=obj_path, expected_digest=None, actual_digest=actual
            )
        return None

    alg = DigestAlgorithm.from_ocfl_name(plan.base_digest_algorithm)
    actual = bytes_digest(raw, alg) if raw is not None else None
    if raw is None or actual != plan.base_digest:
        raise StaleHeadError(
            object_path=obj_path,
            expected_digest=plan.base_digest,
            actual_digest=actual,
        )
    base = StoredInventory.from_bytes(raw, alg)
    if plan.inventory_proof is not None and not fsys.exists(
        join_path(obj_path, OTS_CONFIG_PATH)
    ):
        raise PlanMismatchError(
            f"object {plan.object_id!r} does not use the {OTS_EXTENSION} "
            "extension; an inventory proof can only start a new object"
        )
    return base


def _old_sidecar_alg(base: StoredInventory | None, plan: CommitPlan) -> str:
    if base is None or base.digest_algorithm is plan.inventory.digest_algorithm:
        return ""
    return base.digest_algorithm.ocfl_name


def _extension_files(plan: CommitPlan) -> dict[str, bytes]:
    if plan.inventory_proof is None:
        return {}
    files = {proof_path(plan.version): plan.inventory_proof}
    if plan.is_new_object:
        files = {OTS_CONFIG_PATH: OTS_CONFIG_BYTES, **files}
    return files
