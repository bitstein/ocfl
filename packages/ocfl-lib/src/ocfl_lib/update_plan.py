"""UpdatePlan data structures, serialization, and step construction."""

import base64
import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import msgspec

from ocfl_lib.digest import DigestAlgorithm
from ocfl_lib.fs import utils as fs_utils
from ocfl_lib.inventory import StoredInventory
from ocfl_lib.spec import Namaste, Spec, VNum

if TYPE_CHECKING:
    from collections.abc import Callable

    from ocfl_lib.fs.protocols import FS, WriteFS


@runtime_checkable
class ContentSource(Protocol):
    """Protocol for resolving content by digest."""

    def get_content(self, digest: str) -> tuple[FS, str]: ...


@dataclass
class PlanStep:
    """Single step in an update plan."""

    name: str
    content_digest: str = ""
    is_async: bool = False
    completed: bool = False
    size: int = 0
    error: str = ""
    revert_error: str = ""
    # Not serialized:
    run: Callable[[], int] | None = field(default=None, repr=False)
    revert: Callable[[], None] | None = field(default=None, repr=False)


@dataclass
class UpdatePlan:
    """Plan for creating or updating an OCFL object version."""

    steps: list[PlanStep]
    new_inv: StoredInventory
    old_inv: StoredInventory | None
    old_sidecar_alg: str = ""  # Non-empty when algorithm migration happened


# --- Wire format (msgspec structs for serialization) ---


class _StepState(msgspec.Struct):
    """Serializable step state (no callables)."""

    name: str
    content_digest: str = ""
    is_async: bool = False
    completed: bool = False
    size: int = 0
    error: str = ""
    revert_error: str = ""


class _StoredInvState(msgspec.Struct):
    """Serializable StoredInventory state with base64 raw_bytes."""

    raw_bytes_b64: str
    digest: str
    digest_algorithm: str


class _PlanState(msgspec.Struct):
    """Serializable UpdatePlan state."""

    steps: list[_StepState]
    new_inv: _StoredInvState
    old_inv: _StoredInvState | None = None
    old_sidecar_alg: str = ""


def _inv_to_state(stored: StoredInventory) -> _StoredInvState:
    return _StoredInvState(
        raw_bytes_b64=base64.b64encode(stored.raw_bytes).decode("ascii"),
        digest=stored.digest,
        digest_algorithm=stored.digest_algorithm.ocfl_name,
    )


def _state_to_inv(state: _StoredInvState) -> StoredInventory:
    raw = base64.b64decode(state.raw_bytes_b64)
    alg = DigestAlgorithm.from_ocfl_name(state.digest_algorithm)
    return StoredInventory.from_bytes(raw, alg)


def serialize(plan: UpdatePlan) -> bytes:
    """Serialize an UpdatePlan to JSON bytes."""
    step_states = [
        _StepState(
            name=s.name,
            content_digest=s.content_digest,
            is_async=s.is_async,
            completed=s.completed,
            size=s.size,
            error=s.error,
            revert_error=s.revert_error,
        )
        for s in plan.steps
    ]
    state = _PlanState(
        steps=step_states,
        new_inv=_inv_to_state(plan.new_inv),
        old_inv=_inv_to_state(plan.old_inv) if plan.old_inv else None,
        old_sidecar_alg=plan.old_sidecar_alg,
    )
    return msgspec.json.encode(state)


def deserialize(data: bytes) -> UpdatePlan:
    """Deserialize an UpdatePlan from JSON bytes. Callables are None."""
    state = msgspec.json.decode(data, type=_PlanState)
    steps = [
        PlanStep(
            name=s.name,
            content_digest=s.content_digest,
            is_async=s.is_async,
            completed=s.completed,
            size=s.size,
            error=s.error,
            revert_error=s.revert_error,
        )
        for s in state.steps
    ]
    return UpdatePlan(
        steps=steps,
        new_inv=_state_to_inv(state.new_inv),
        old_inv=_state_to_inv(state.old_inv) if state.old_inv else None,
        old_sidecar_alg=state.old_sidecar_alg,
    )


# --- Step construction ---


def _join(base: str, name: str) -> str:
    if not base:
        return name
    return f"{base}/{name}"


def _noop_run() -> int:
    return 0


def _noop_revert() -> None:
    pass


def prepare_steps(
    plan: UpdatePlan,
    fsys: WriteFS,
    obj_dir: str,
    content_source: ContentSource,
) -> None:
    """Build run/revert callables on plan steps from inventory data.

    Populates plan.steps with the full step sequence. Called after
    construction or deserialization to make the plan executable.

    On resume (plan.steps non-empty from deserialization): validates
    rebuilt steps match existing by (name, content_digest), then
    transfers completed/error state from old steps to new.
    """
    new_inv = plan.new_inv
    old_inv = plan.old_inv
    inv = new_inv.inventory
    spec = Spec.from_inventory_type(inv.type)
    vnum = VNum.parse(inv.head)
    vnum_str = str(vnum)
    version_dir = _join(obj_dir, vnum_str)
    is_new = old_inv is None

    old_manifest = old_inv.inventory.manifest if old_inv else {}

    steps: list[PlanStep] = []

    # 1. Object root (v1 only)
    if is_new:
        steps.append(
            PlanStep(
                name="object-root",
                run=_noop_run,
                revert=lambda: fsys.remove_all(obj_dir),
            )
        )

    # 2. Declaration
    namaste = Namaste.for_object(spec)
    namaste_path = _join(obj_dir, namaste.filename)
    namaste_bytes = namaste.content.encode()

    def _write_declaration() -> int:
        fsys.mkdir(obj_dir)
        return fs_utils.write_bytes(fsys, namaste_path, namaste_bytes)

    if is_new:

        def _revert_declaration() -> None:
            with contextlib.suppress(FileNotFoundError):
                fsys.remove(namaste_path)
    else:
        _revert_declaration = _noop_revert

    steps.append(
        PlanStep(
            name="declaration",
            run=_write_declaration,
            revert=_revert_declaration,
        )
    )

    # 3. Version directory
    def _make_version_dir() -> int:
        fsys.mkdir(version_dir)
        return 0

    steps.append(
        PlanStep(
            name="version-dir",
            run=_make_version_dir,
            revert=lambda: fsys.remove_all(version_dir),
        )
    )

    # 4. Content copies (async)
    for digest, content_paths in inv.manifest.items():
        if digest in old_manifest:
            continue
        content_path = content_paths[0]
        dest = _join(obj_dir, content_path)

        def _make_copy(d: str = digest, dst: str = dest) -> int:
            # Ensure parent directory exists
            parent = dst.rsplit("/", 1)[0] if "/" in dst else ""
            if parent:
                fsys.mkdir(parent)
            src_fs, src_path = content_source.get_content(d)
            return fs_utils.copy(src_fs, src_path, fsys, dst)

        def _make_revert(dst: str = dest) -> None:
            with contextlib.suppress(FileNotFoundError):
                fsys.remove(dst)

        steps.append(
            PlanStep(
                name="content-copy",
                content_digest=digest,
                is_async=True,
                run=_make_copy,
                revert=_make_revert,
            )
        )

    # 5. Version inventory.json
    vinv_path = _join(version_dir, "inventory.json")
    steps.append(
        PlanStep(
            name="version-inv",
            run=lambda: fs_utils.write_bytes(fsys, vinv_path, new_inv.raw_bytes),
            revert=lambda: _try_remove(fsys, vinv_path),
        )
    )

    # 6. Version sidecar
    vsidecar_path = _join(version_dir, new_inv.sidecar_filename)
    vsidecar_bytes = new_inv.sidecar_content.encode()
    steps.append(
        PlanStep(
            name="version-sidecar",
            run=lambda: fs_utils.write_bytes(fsys, vsidecar_path, vsidecar_bytes),
            revert=lambda: _try_remove(fsys, vsidecar_path),
        )
    )

    # 7. Root inventory.json
    rinv_path = _join(obj_dir, "inventory.json")
    steps.append(
        PlanStep(
            name="root-inv",
            run=lambda: fs_utils.write_bytes(fsys, rinv_path, new_inv.raw_bytes),
            revert=lambda: _try_remove(fsys, rinv_path),
        )
    )

    # 8. Root sidecar (last step — marks completion)
    rsidecar_path = _join(obj_dir, new_inv.sidecar_filename)
    rsidecar_bytes = new_inv.sidecar_content.encode()
    steps.append(
        PlanStep(
            name="root-sidecar",
            run=lambda: fs_utils.write_bytes(fsys, rsidecar_path, rsidecar_bytes),
            revert=lambda: _try_remove(fsys, rsidecar_path),
        )
    )

    # 9. Remove old root sidecar on algorithm migration
    if plan.old_sidecar_alg:
        old_sidecar_path = _join(obj_dir, f"inventory.json.{plan.old_sidecar_alg}")

        def _remove_old_sidecar() -> int:
            with contextlib.suppress(FileNotFoundError):
                fsys.remove(old_sidecar_path)
            return 0

        steps.append(
            PlanStep(
                name="remove-old-sidecar",
                run=_remove_old_sidecar,
                revert=_noop_revert,
            )
        )

    # On resume: validate new steps match existing, transfer completed state
    if plan.steps:
        old_steps = plan.steps
        if len(old_steps) != len(steps):
            raise ValueError(
                f"plan step count mismatch: existing {len(old_steps)}, "
                f"rebuilt {len(steps)}"
            )
        for old, new in zip(old_steps, steps, strict=False):
            if old.name != new.name or old.content_digest != new.content_digest:
                raise ValueError(
                    f"plan step mismatch: existing ({old.name!r}, "
                    f"{old.content_digest!r}) != rebuilt ({new.name!r}, "
                    f"{new.content_digest!r})"
                )
            new.completed = old.completed
            new.size = old.size
            # Only transfer error/revert_error for completed steps;
            # incomplete steps with stale errors will be re-run clean
            if old.completed:
                new.error = old.error
                new.revert_error = old.revert_error

    plan.steps = steps


def _try_remove(fsys: WriteFS, path: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        fsys.remove(path)


# --- Plan execution ---


def execute_plan(
    plan: UpdatePlan,
    *,
    max_workers: int | None = None,
) -> None:
    """Execute all steps in the plan.

    Async steps (content copies) run concurrently in a ThreadPoolExecutor.
    Sync steps run sequentially after the async batch.
    Steps already marked completed are skipped.
    On failure: sets step.error and re-raises.
    """
    from concurrent.futures import ThreadPoolExecutor

    # Partition steps into pre-async sync, async, and post-async sync
    pre_async: list[PlanStep] = []
    async_steps: list[PlanStep] = []
    post_async: list[PlanStep] = []
    seen_async = False
    for step in plan.steps:
        if step.completed:
            continue
        if step.is_async:
            seen_async = True
            async_steps.append(step)
        elif not seen_async:
            pre_async.append(step)
        else:
            post_async.append(step)

    # Execute pre-async sync steps (object-root, declaration, version-dir)
    for step in pre_async:
        _run_step(step)

    # Execute async steps (content copies) concurrently
    if async_steps:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_run_step, s): s for s in async_steps}
            for future in futures:
                future.result()  # re-raises on error

    # Execute post-async sync steps (inventories, sidecars)
    for step in post_async:
        _run_step(step)


def _run_step(step: PlanStep) -> None:
    """Execute a single step, marking it completed or setting error."""
    if step.completed:
        return
    assert step.run is not None, f"step {step.name!r} has no run callable"
    try:
        step.size = step.run()
        step.completed = True
    except Exception as exc:
        step.error = str(exc)
        raise


# --- Plan revert ---


def revert_plan(plan: UpdatePlan) -> None:
    """Revert completed steps in reverse order.

    Calls each completed step's revert callable. On revert failure,
    sets step.revert_error and continues reverting remaining steps.
    """
    for step in reversed(plan.steps):
        if not step.completed:
            continue
        if step.revert is None:
            continue
        try:
            step.revert()
        except Exception as exc:
            step.revert_error = str(exc)
