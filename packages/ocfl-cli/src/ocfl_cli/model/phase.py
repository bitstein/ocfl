"""OTS phase FSM for staged OCFL versions."""

import enum
from typing import Any

from ocfl_cli.errors import PhaseError


class TimestampTarget(enum.StrEnum):
    """What a timestamp proves: a version's content files or its inventory."""

    CONTENT = "content"
    INVENTORY = "inventory"


class Phase(enum.StrEnum):
    """Lifecycle phase of an OTS-enabled staging directory.

    Persisted as a scalar in `ots.json.phase`. Legality + auto-regression
    are enforced by `check_transition`.
    """

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
    """The Timestamp Target a Staging Area in `phase` works on (None once committed)."""
    return _TARGETS[phase]


# Sentinels for the transition matrix.
_STAY = object()  # outcome: phase unchanged
_ERR = object()  # outcome: PhaseError
_REGRESS = object()  # outcome: Phase.DRAFTING

_EDITABLE = (
    Phase.DRAFTING,
    Phase.CONTENT_PENDING,
    Phase.CONTENT_UPGRADED,
    Phase.INVENTORY_PENDING,
    Phase.INVENTORY_UPGRADED,
)

# (phase, command) -> Phase | sentinel. A timestamp command is
# "<verb> <target>": the verb is the `stage` command (`timestamp`, `upgrade`,
# `retry`), the target the one it works on.
# Mirrors `tests/test_staging_phase.py::MATRIX`.
_TRANSITIONS: dict[tuple[Phase, str], Any] = {
    # add / rm / mv: stay at drafting + content-pending; regress from upgraded.
    **{
        (p, c): outcome
        for c in ("add", "rm", "mv")
        for p, outcome in zip(
            _EDITABLE, (_STAY, _STAY, _REGRESS, _REGRESS, _REGRESS), strict=True
        )
    },
    # timestamp content
    **dict(
        zip(
            ((p, "timestamp content") for p in _EDITABLE),
            (Phase.CONTENT_PENDING, _STAY, _STAY, _ERR, _ERR),  # upgraded: noop
            strict=True,
        )
    ),
    # upgrade content
    **dict(
        zip(
            ((p, "upgrade content") for p in _EDITABLE),
            (_ERR, _STAY, _STAY, _ERR, _ERR),  # upgraded: noop
            strict=True,
        )
    ),
    # retry content: only valid at content-pending.
    **dict(
        zip(
            ((p, "retry content") for p in _EDITABLE),
            (_ERR, _STAY, _ERR, _ERR, _ERR),
            strict=True,
        )
    ),
    # timestamp inventory: pending resubmits, upgraded is a noop.
    **dict(
        zip(
            ((p, "timestamp inventory") for p in _EDITABLE),
            (_ERR, _ERR, Phase.INVENTORY_PENDING, _STAY, _STAY),
            strict=True,
        )
    ),
    # upgrade inventory
    **dict(
        zip(
            ((p, "upgrade inventory") for p in _EDITABLE),
            (_ERR, _ERR, _ERR, Phase.INVENTORY_UPGRADED, _STAY),  # upgraded: noop
            strict=True,
        )
    ),
    # retry inventory: only valid at inventory-pending.
    **dict(
        zip(
            ((p, "retry inventory") for p in _EDITABLE),
            (_ERR, _ERR, _ERR, _STAY, _ERR),
            strict=True,
        )
    ),
    # commit (ots path): only valid at inventory-upgraded.
    **dict(
        zip(
            ((p, "commit (ots path)") for p in _EDITABLE),
            (_ERR, _ERR, _ERR, _ERR, Phase.COMMITTED),
            strict=True,
        )
    ),
    # reset / status: always legal, phase unchanged.
    **{(p, c): _STAY for c in ("reset", "status") for p in _EDITABLE},
}


def is_legal(phase: Phase, command: str) -> bool:
    """Whether `command` may run in `phase` (see `check_transition`)."""
    outcome = _TRANSITIONS.get((phase, command), _ERR)
    return outcome is not _ERR


def check_transition(phase: Phase, command: str) -> Phase:
    """Return the phase resulting from running *command* in *phase*.

    Raises `PhaseError` for illegal transitions or unknown commands.
    `regress-*` outcomes return `Phase.DRAFTING`. Caller is responsible
    for applying any state mutation associated with regression.
    """
    try:
        outcome = _TRANSITIONS[(phase, command)]
    except KeyError:
        raise PhaseError(
            f"command {command!r} not legal in phase {phase.value!r}"
        ) from None
    if outcome is _ERR:
        raise PhaseError(f"command {command!r} not legal in phase {phase.value!r}")
    if outcome is _STAY:
        return phase
    if outcome is _REGRESS:
        return Phase.DRAFTING
    return outcome  # explicit Phase target (advance)
