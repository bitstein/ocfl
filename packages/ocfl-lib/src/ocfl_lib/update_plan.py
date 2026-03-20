"""UpdatePlan data structures and serialization for OCFL write operations."""

import base64
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import msgspec

from ocfl_lib.digest import DigestAlgorithm
from ocfl_lib.inventory import StoredInventory

if TYPE_CHECKING:
    from collections.abc import Callable

    from ocfl_lib.fs.protocols import FS


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
    )
