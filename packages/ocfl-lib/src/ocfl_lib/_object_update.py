"""Write a new version of an OCFL object.

`Object` only reads. These functions plan and apply a version update.
"""

from typing import TYPE_CHECKING

from ocfl_lib._commit import commit, executor_plan
from ocfl_lib._commit_plan import build_plan
from ocfl_lib._errors import InventoryBuildError
from ocfl_lib._object import Object
from ocfl_lib._update_plan import execute_plan, prepare_steps

if TYPE_CHECKING:
    from ocfl_lib._inventory import UserInfo
    from ocfl_lib._sources import ContentSource
    from ocfl_lib._spec import Spec
    from ocfl_lib._update_plan import UpdatePlan
    from ocfl_lib._version_state import VersionState
    from ocfl_lib.fs._protocols import WriteFS


def update_object(
    fsys: WriteFS,
    obj: Object,
    version_state: VersionState,
    *,
    id: str | None = None,
    spec: Spec | None = None,
    message: str | None = None,
    user: UserInfo | None = None,
    created: str | None = None,
) -> Object:
    """Create or update an OCFL object version.

    For new objects (not obj.exists), id is required.
    On error: reverts completed steps, then re-raises.
    Returns a fresh Object for the new head.
    """
    plan = build_plan(
        obj,
        version_state,
        object_id=_object_id(obj, id),
        spec=spec,
        message=message,
        user=user,
        created=created,
    )
    return commit(fsys, obj, plan, version_state.content_source)


def plan_update(
    obj: Object,
    version_state: VersionState,
    *,
    id: str | None = None,
    spec: Spec | None = None,
    message: str | None = None,
    user: UserInfo | None = None,
    created: str | None = None,
) -> UpdatePlan:
    """Build an update plan without executing it."""
    plan = build_plan(
        obj,
        version_state,
        object_id=_object_id(obj, id),
        spec=spec,
        message=message,
        user=user,
        created=created,
    )
    return executor_plan(obj, plan)


def apply_update_plan(
    fsys: WriteFS,
    obj: Object,
    plan: UpdatePlan,
    content_source: ContentSource,
) -> Object:
    """Execute a previously created update plan.

    No auto-revert — caller handles errors and decides whether to revert.
    Returns a fresh Object for the new head.
    """
    prepare_steps(plan, fsys, obj.path, content_source)
    execute_plan(plan)
    return Object(obj.fs, obj.path)


def _object_id(obj: Object, id: str | None) -> str:
    if id is not None:
        return id
    if not obj.exists:
        raise InventoryBuildError("id is required to create a new object")
    return obj.id
