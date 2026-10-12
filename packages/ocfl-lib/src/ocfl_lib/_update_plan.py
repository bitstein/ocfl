"""In-memory update steps that write a CommitPlan into an object.

## Cancellation and recovery

``ocfl_lib`` is a transactional library: ``StorageRoot.commit`` auto-reverts
on any ``Exception`` or ``KeyboardInterrupt``, leaving the object directory in
its pre-update state before re-raising. Same convention as SQLAlchemy,
SQLite's ``with conn:``, and Django's ``atomic()``.

Any other ``BaseException`` (``SystemExit``, ``MemoryError``) propagates
without cleanup.

A process killed hard (SIGKILL, power loss, OOM) cannot be cleanly
recovered: partial state sits on disk and requires manual cleanup.
"""

import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ocfl_lib._digest import bytes_digest
from ocfl_lib._errors import (
    RevertCompletedError,
    RevertError,
    StaleHeadError,
)
from ocfl_lib._path import join_path
from ocfl_lib._spec import Namaste, Spec, VNum
from ocfl_lib.fs import _utils as fs_utils

if TYPE_CHECKING:
    from collections.abc import Callable

    from ocfl_lib._inventory import StoredInventory
    from ocfl_lib._sources import ContentSource
    from ocfl_lib.fs._protocols import FS, WriteFS

__all__ = [
    "PlanStep",
    "RevertError",
    "UpdatePlan",
    "execute_plan",
    "prepare_steps",
    "revert_plan",
]


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
    run: Callable[[], int] | None = field(default=None, repr=False)
    revert: Callable[[], None] | None = field(default=None, repr=False)


@dataclass
class UpdatePlan:
    """Plan for creating or updating an OCFL object version.

    **Non-atomic completion caveat:** ``step.run()`` followed by
    ``step.completed = True`` is not atomic. Steps live in memory only;
    a process that dies mid-plan leaves partial state for manual cleanup.
    """

    steps: list[PlanStep]
    new_inv: StoredInventory
    old_inv: StoredInventory | None
    old_sidecar_alg: str = ""  # Non-empty when algorithm migration happened
    revert_inv: StoredInventory | None = None  # Original pre-migration inv for revert
    extension_files: dict[str, bytes] = field(default_factory=dict)
    expected_root_sidecar_digest: str | None = None
    expected_root_sidecar_algorithm: str = ""

    def __post_init__(self) -> None:
        if self.old_inv is not None and self.expected_root_sidecar_digest is None:
            self.expected_root_sidecar_digest = self.old_inv.digest
            self.expected_root_sidecar_algorithm = (
                self.old_inv.digest_algorithm.ocfl_name
            )


# --- Step construction ---


def _noop_run() -> int:
    return 0


def _noop_revert() -> None:
    pass


def _read_sidecar_digest(fsys: FS, path: str) -> str | None:
    try:
        parts = fs_utils.read_text(fsys, path).split(maxsplit=1)
    except FileNotFoundError:
        return None
    return parts[0] if parts else None


def _expected_root_sidecar_path(plan: UpdatePlan, obj_dir: str) -> str:
    if plan.expected_root_sidecar_algorithm:
        alg = plan.expected_root_sidecar_algorithm
        return join_path(obj_dir, f"inventory.json.{alg}")
    return join_path(obj_dir, plan.new_inv.sidecar_filename)


def _head_matches_expected(plan: UpdatePlan, fsys: FS, obj_dir: str) -> bool:
    return (
        _read_sidecar_digest(fsys, _expected_root_sidecar_path(plan, obj_dir))
        == plan.expected_root_sidecar_digest
    )


def _head_matches_plan(plan: UpdatePlan, fsys: FS, obj_dir: str) -> bool:
    return (
        _read_sidecar_digest(fsys, join_path(obj_dir, plan.new_inv.sidecar_filename))
        == plan.new_inv.digest
    )


def _read_root_inventory_digest(plan: UpdatePlan, fsys: FS, obj_dir: str) -> str | None:
    try:
        data = fs_utils.read_all(fsys, join_path(obj_dir, "inventory.json"))
    except FileNotFoundError:
        return None
    return bytes_digest(data, plan.new_inv.digest_algorithm)


def _check_planned_root_inventory(
    plan: UpdatePlan,
    fsys: FS,
    obj_dir: str,
) -> None:
    actual = _read_root_inventory_digest(plan, fsys, obj_dir)
    if actual != plan.new_inv.digest:
        raise StaleHeadError(
            object_path=obj_dir,
            expected_digest=plan.new_inv.digest,
            actual_digest=actual,
        )


def _check_stale_head(
    plan: UpdatePlan,
    fsys: FS,
    obj_dir: str,
    *,
    allow_planned_root_inventory: bool = False,
) -> None:
    expected = plan.expected_root_sidecar_digest
    if expected is None:
        inv_path = join_path(obj_dir, "inventory.json")
        sidecar_path = join_path(obj_dir, plan.new_inv.sidecar_filename)
        if (not allow_planned_root_inventory and fsys.exists(inv_path)) or fsys.exists(
            sidecar_path
        ):
            raise StaleHeadError(
                object_path=obj_dir,
                expected_digest=None,
                actual_digest=_read_sidecar_digest(fsys, sidecar_path),
            )
        return

    actual = _read_sidecar_digest(fsys, _expected_root_sidecar_path(plan, obj_dir))
    if actual != expected:
        raise StaleHeadError(
            object_path=obj_dir,
            expected_digest=expected,
            actual_digest=actual,
        )


def prepare_steps(
    plan: UpdatePlan,
    fsys: WriteFS,
    obj_dir: str,
    content_source: ContentSource,
) -> None:
    """Build run/revert callables on plan steps from inventory data."""
    new_inv = plan.new_inv
    old_inv = plan.old_inv
    revert_src = plan.revert_inv or old_inv  # Original inv for revert data
    inv = new_inv.inventory
    spec = Spec.from_inventory_type(inv.type)
    vnum = VNum.parse(inv.head)
    vnum_str = str(vnum)
    version_dir = join_path(obj_dir, vnum_str)
    is_new = old_inv is None

    steps: list[PlanStep] = []

    # 1. Object root (v1 only)
    if is_new:

        def _revert_object_root() -> None:
            with contextlib.suppress(FileNotFoundError):
                fsys.remove_all(obj_dir)

        steps.append(
            PlanStep(
                name="object-root",
                run=_noop_run,
                revert=_revert_object_root,
            )
        )

    # 2. Declaration
    namaste = Namaste.for_object(spec)
    namaste_path = join_path(obj_dir, namaste.filename)
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

    # Spec upgrade: the declaration is swapped just before root-inv (step 7b)
    # so the object keeps exactly one NAMASTE (E003) matching its root
    # inventory type (E038) for as long as possible.
    old_spec = Spec.from_inventory_type(old_inv.inventory.type) if old_inv else None
    is_upgrade = old_spec is not None and old_spec != spec
    if not is_upgrade:
        steps.append(
            PlanStep(
                name="declaration",
                run=_write_declaration,
                revert=_revert_declaration,
            )
        )

    # 3. Version directory
    def _make_version_dir() -> int:
        _check_stale_head(plan, fsys, obj_dir)
        fsys.mkdir(version_dir)
        return 0

    def _revert_version_dir() -> None:
        with contextlib.suppress(FileNotFoundError):
            fsys.remove_all(version_dir)

    steps.append(
        PlanStep(
            name="version-dir",
            run=_make_version_dir,
            revert=_revert_version_dir,
        )
    )

    # 4. Content copies (async): the content paths of the new version
    new_content = [
        (digest, path)
        for digest, paths in inv.manifest.items()
        for path in paths
        if path.startswith(f"{vnum_str}/")
    ]
    for digest, content_path in new_content:
        dest = join_path(obj_dir, content_path)

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
    vinv_path = join_path(version_dir, "inventory.json")
    steps.append(
        PlanStep(
            name="version-inv",
            run=lambda: fs_utils.write_bytes(fsys, vinv_path, new_inv.raw_bytes),
            revert=lambda: _try_remove(fsys, vinv_path),
        )
    )

    # 6. Version sidecar
    vsidecar_path = join_path(version_dir, new_inv.sidecar_filename)
    vsidecar_bytes = new_inv.sidecar_content.encode()
    steps.append(
        PlanStep(
            name="version-sidecar",
            run=lambda: fs_utils.write_bytes(fsys, vsidecar_path, vsidecar_bytes),
            revert=lambda: _try_remove(fsys, vsidecar_path),
        )
    )

    # 7. Extension files
    for rel_path, content_bytes in plan.extension_files.items():
        dest = join_path(obj_dir, rel_path)

        def _make_ext_write(dst: str = dest, data: bytes = content_bytes) -> int:
            parent = dst.rsplit("/", 1)[0] if "/" in dst else ""
            if parent:
                fsys.mkdir(parent)
            return fs_utils.write_bytes(fsys, dst, data)

        def _make_ext_revert(dst: str = dest) -> None:
            _try_remove(fsys, dst)

        steps.append(
            PlanStep(
                name="extension-file",
                run=_make_ext_write,
                revert=_make_ext_revert,
            )
        )

    # 7b. Declaration swap (spec upgrade only)
    if old_spec is not None and is_upgrade:
        old_namaste = Namaste.for_object(old_spec)
        old_namaste_path = join_path(obj_dir, old_namaste.filename)
        old_namaste_bytes = old_namaste.content.encode()

        def _swap_declaration() -> int:
            n = fs_utils.write_bytes(fsys, namaste_path, namaste_bytes)
            fsys.remove(old_namaste_path)
            return n

        def _revert_swap_declaration() -> None:
            fs_utils.write_bytes(fsys, old_namaste_path, old_namaste_bytes)
            _try_remove(fsys, namaste_path)

        steps.append(
            PlanStep(
                name="declaration-swap",
                run=_swap_declaration,
                revert=_revert_swap_declaration,
            )
        )

    # 8. Root inventory.json
    rinv_path = join_path(obj_dir, "inventory.json")
    if revert_src is not None:
        revert_rinv_bytes = revert_src.raw_bytes
        revert_sidecar_path_for_root_inv = join_path(
            obj_dir, revert_src.sidecar_filename
        )
        revert_sidecar_bytes_for_root_inv = revert_src.sidecar_content.encode()

        def _revert_root_inv() -> None:
            if _head_matches_plan(plan, fsys, obj_dir):
                _try_remove(fsys, join_path(obj_dir, plan.new_inv.sidecar_filename))
                fs_utils.write_bytes(
                    fsys,
                    revert_sidecar_path_for_root_inv,
                    revert_sidecar_bytes_for_root_inv,
                )
                fs_utils.write_bytes(fsys, rinv_path, revert_rinv_bytes)
            elif _head_matches_expected(plan, fsys, obj_dir):
                fs_utils.write_bytes(fsys, rinv_path, revert_rinv_bytes)
    else:

        def _revert_root_inv() -> None:
            if _head_matches_plan(plan, fsys, obj_dir):
                _try_remove(fsys, join_path(obj_dir, plan.new_inv.sidecar_filename))
                _try_remove(fsys, rinv_path)
            elif _head_matches_expected(plan, fsys, obj_dir):
                _try_remove(fsys, rinv_path)

    steps.append(
        PlanStep(
            name="root-inv",
            run=lambda: _write_root_inventory(plan, fsys, obj_dir, rinv_path),
            revert=_revert_root_inv,
        )
    )

    # 9. Root sidecar (last step — marks completion)
    rsidecar_path = join_path(obj_dir, new_inv.sidecar_filename)
    rsidecar_bytes = new_inv.sidecar_content.encode()
    if revert_src is not None:
        revert_sidecar_path = join_path(obj_dir, revert_src.sidecar_filename)
        revert_sidecar_bytes = revert_src.sidecar_content.encode()

        def _revert_root_sidecar() -> None:
            _try_remove(fsys, rsidecar_path)
            fs_utils.write_bytes(fsys, revert_sidecar_path, revert_sidecar_bytes)
    else:

        def _revert_root_sidecar() -> None:
            _try_remove(fsys, rsidecar_path)

    steps.append(
        PlanStep(
            name="root-sidecar",
            run=lambda: _write_root_sidecar(
                plan, fsys, obj_dir, rsidecar_path, rsidecar_bytes
            ),
            revert=_revert_root_sidecar,
        )
    )

    # 10. Remove old root sidecar on algorithm migration
    if plan.old_sidecar_alg:
        migrated_sidecar_path = join_path(
            obj_dir, f"inventory.json.{plan.old_sidecar_alg}"
        )

        def _remove_old_sidecar() -> int:
            with contextlib.suppress(FileNotFoundError):
                fsys.remove(migrated_sidecar_path)
            return 0

        steps.append(
            PlanStep(
                name="remove-old-sidecar",
                run=_remove_old_sidecar,
                revert=_noop_revert,
            )
        )

    plan.steps = steps


def _try_remove(fsys: WriteFS, path: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        fsys.remove(path)


def _write_root_inventory(
    plan: UpdatePlan,
    fsys: WriteFS,
    obj_dir: str,
    rinv_path: str,
) -> int:
    _check_stale_head(plan, fsys, obj_dir)
    return fs_utils.write_bytes(fsys, rinv_path, plan.new_inv.raw_bytes)


def _write_root_sidecar(
    plan: UpdatePlan,
    fsys: WriteFS,
    obj_dir: str,
    rsidecar_path: str,
    rsidecar_bytes: bytes,
) -> int:
    _check_stale_head(plan, fsys, obj_dir, allow_planned_root_inventory=True)
    # The root sidecar certifies the root inventory. Re-read storage here so a
    # racing writer cannot replace inventory.json between our root-inv and
    # root-sidecar steps without being detected.
    _check_planned_root_inventory(plan, fsys, obj_dir)
    return fs_utils.write_bytes(fsys, rsidecar_path, rsidecar_bytes)


# --- Plan execution ---


def execute_plan(
    plan: UpdatePlan,
    *,
    max_workers: int | None = None,
) -> None:
    """Execute all steps in the plan.

    Async steps (content copies) run concurrently via bounded_map.
    Sync steps run sequentially after the async batch.
    Steps already marked completed are skipped.
    On failure: sets step.error and re-raises.
    """
    from ocfl_lib._concurrent import bounded_map

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
        for _ in bounded_map(_run_step, async_steps, max_workers=max_workers):
            pass

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
    Raises RevertError if any revert failed (after all reverts attempted).
    Raises RevertCompletedError if every step is already completed
    (refuses to undo a successful commit).
    """
    has_error = any(s.error for s in plan.steps)
    if plan.steps and all(s.completed for s in plan.steps) and not has_error:
        raise RevertCompletedError(
            "refusing to revert: all plan steps completed successfully"
        )
    failures: list[tuple[str, str]] = []
    for step in reversed(plan.steps):
        if not step.completed:
            continue
        if step.revert is None:
            continue
        try:
            step.revert()
        except Exception as exc:
            step.revert_error = str(exc)
            failures.append((step.name, str(exc)))
    if failures:
        raise RevertError(tuple(failures))
