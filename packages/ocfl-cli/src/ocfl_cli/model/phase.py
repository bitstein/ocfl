import enum
from typing import Literal, Self

from ocfl_cli.errors import PhaseError


class TimestampTarget(enum.StrEnum):
    CONTENT = "content"
    INVENTORY = "inventory"


class Phase(enum.StrEnum):
    DRAFTING = "drafting"
    CONTENT_PENDING = "content-pending"
    CONTENT_UPGRADED = "content-upgraded"
    INVENTORY_PENDING = "inventory-pending"
    INVENTORY_UPGRADED = "inventory-upgraded"
    COMMITTED = "committed"


_TARGETS: dict[Phase, TimestampTarget | None] = {
    Phase.DRAFTING: TimestampTarget.CONTENT,
    Phase.CONTENT_PENDING: TimestampTarget.CONTENT,
    Phase.CONTENT_UPGRADED: TimestampTarget.INVENTORY,
    Phase.INVENTORY_PENDING: TimestampTarget.INVENTORY,
    Phase.INVENTORY_UPGRADED: TimestampTarget.INVENTORY,
    Phase.COMMITTED: None,
}


def target_for(phase: Phase) -> TimestampTarget | None:
    return _TARGETS[phase]


type TimestampVerb = Literal["timestamp", "upgrade", "retry"]


class PhaseCommand(enum.StrEnum):
    ADD = "add"
    RM = "rm"
    MV = "mv"
    TIMESTAMP_CONTENT = "timestamp content"
    UPGRADE_CONTENT = "upgrade content"
    RETRY_CONTENT = "retry content"
    TIMESTAMP_INVENTORY = "timestamp inventory"
    UPGRADE_INVENTORY = "upgrade inventory"
    RETRY_INVENTORY = "retry inventory"
    COMMIT = "commit (ots path)"
    DISCARD = "discard"
    STATUS = "status"

    @classmethod
    def targeted(cls, verb: TimestampVerb, target: TimestampTarget) -> Self:
        return cls(f"{verb} {target.value}")


_D = Phase.DRAFTING
_CP = Phase.CONTENT_PENDING
_CU = Phase.CONTENT_UPGRADED
_IP = Phase.INVENTORY_PENDING
_IU = Phase.INVENTORY_UPGRADED

_EDIT = {_D: _D, _CP: _CP, _CU: _D, _IP: _D, _IU: _D}
_UNCHANGED = {_D: _D, _CP: _CP, _CU: _CU, _IP: _IP, _IU: _IU}

_NEXT_PHASE: dict[PhaseCommand, dict[Phase, Phase]] = {
    PhaseCommand.ADD: _EDIT,
    PhaseCommand.RM: _EDIT,
    PhaseCommand.MV: _EDIT,
    PhaseCommand.TIMESTAMP_CONTENT: {_D: _CP, _CP: _CP, _CU: _CU},
    PhaseCommand.UPGRADE_CONTENT: {_CP: _CP, _CU: _CU},
    PhaseCommand.RETRY_CONTENT: {_CP: _CP},
    PhaseCommand.TIMESTAMP_INVENTORY: {_CU: _IP, _IP: _IP, _IU: _IU},
    PhaseCommand.UPGRADE_INVENTORY: {_IP: _IU, _IU: _IU},
    PhaseCommand.RETRY_INVENTORY: {_IP: _IP},
    PhaseCommand.COMMIT: {_IU: Phase.COMMITTED},
    PhaseCommand.DISCARD: _UNCHANGED,
    PhaseCommand.STATUS: _UNCHANGED,
}


def is_legal(phase: Phase, command: PhaseCommand) -> bool:
    return phase in _NEXT_PHASE[command]


def check_transition(phase: Phase, command: PhaseCommand) -> Phase:
    next_phase = _NEXT_PHASE[command].get(phase)
    if next_phase is None:
        raise PhaseError(
            f"command {command.value!r} not legal in phase {phase.value!r}"
        )
    return next_phase
