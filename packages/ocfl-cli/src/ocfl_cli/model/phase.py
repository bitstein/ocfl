"""OTS phase FSM for staged OCFL versions."""

import enum
from typing import Any

from ocfl_cli.errors import PhaseError


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


# Sentinels for the transition matrix.
_STAY = object()  # outcome: phase unchanged
_ERR = object()  # outcome: PhaseError
_REGRESS = object()  # outcome: Phase.DRAFTING

# (phase, command) -> Phase | sentinel.
# Mirrors `architecture.transitions.matrix` and `tests/test_staging_phase.py::MATRIX`.
_TRANSITIONS: dict[tuple[Phase, str], Any] = {
    # add / rm / mv: stay at drafting + content-pending; regress from upgraded.
    **{
        (p, c): outcome
        for c in ("add", "rm", "mv")
        for p, outcome in (
            (Phase.DRAFTING, _STAY),
            (Phase.CONTENT_PENDING, _STAY),
            (Phase.CONTENT_UPGRADED, _REGRESS),
            (Phase.INVENTORY_PENDING, _REGRESS),
            (Phase.INVENTORY_UPGRADED, _REGRESS),
        )
    },
    # timestamp content
    (Phase.DRAFTING, "timestamp content"): Phase.CONTENT_PENDING,
    (Phase.CONTENT_PENDING, "timestamp content"): _STAY,
    (Phase.CONTENT_UPGRADED, "timestamp content"): _STAY,  # noop
    (Phase.INVENTORY_PENDING, "timestamp content"): _ERR,
    (Phase.INVENTORY_UPGRADED, "timestamp content"): _ERR,
    # timestamp content --upgrade [--watch]
    **{
        (p, c): outcome
        for c in (
            "timestamp content --upgrade",
            "timestamp content --upgrade --watch",
        )
        for p, outcome in (
            (Phase.DRAFTING, _ERR),
            (Phase.CONTENT_PENDING, _STAY),
            (Phase.CONTENT_UPGRADED, _STAY),  # noop
            (Phase.INVENTORY_PENDING, _ERR),
            (Phase.INVENTORY_UPGRADED, _ERR),
        )
    },
    # timestamp content --retry: only valid at content-pending.
    (Phase.DRAFTING, "timestamp content --retry"): _ERR,
    (Phase.CONTENT_PENDING, "timestamp content --retry"): _STAY,
    (Phase.CONTENT_UPGRADED, "timestamp content --retry"): _ERR,
    (Phase.INVENTORY_PENDING, "timestamp content --retry"): _ERR,
    (Phase.INVENTORY_UPGRADED, "timestamp content --retry"): _ERR,
    # timestamp inventory
    (Phase.DRAFTING, "timestamp inventory"): _ERR,
    (Phase.CONTENT_PENDING, "timestamp inventory"): _ERR,
    (Phase.CONTENT_UPGRADED, "timestamp inventory"): Phase.INVENTORY_PENDING,
    (Phase.INVENTORY_PENDING, "timestamp inventory"): _STAY,  # resubmit
    (Phase.INVENTORY_UPGRADED, "timestamp inventory"): _STAY,  # noop
    # timestamp inventory --upgrade [--watch]
    **{
        (p, c): outcome
        for c in (
            "timestamp inventory --upgrade",
            "timestamp inventory --upgrade --watch",
        )
        for p, outcome in (
            (Phase.DRAFTING, _ERR),
            (Phase.CONTENT_PENDING, _ERR),
            (Phase.CONTENT_UPGRADED, _ERR),
            (Phase.INVENTORY_PENDING, Phase.INVENTORY_UPGRADED),
            (Phase.INVENTORY_UPGRADED, _STAY),  # noop
        )
    },
    # timestamp inventory --retry: only valid at inventory-pending.
    (Phase.DRAFTING, "timestamp inventory --retry"): _ERR,
    (Phase.CONTENT_PENDING, "timestamp inventory --retry"): _ERR,
    (Phase.CONTENT_UPGRADED, "timestamp inventory --retry"): _ERR,
    (Phase.INVENTORY_PENDING, "timestamp inventory --retry"): _STAY,
    (Phase.INVENTORY_UPGRADED, "timestamp inventory --retry"): _ERR,
    # commit (ots path): only valid at inventory-upgraded.
    (Phase.DRAFTING, "commit (ots path)"): _ERR,
    (Phase.CONTENT_PENDING, "commit (ots path)"): _ERR,
    (Phase.CONTENT_UPGRADED, "commit (ots path)"): _ERR,
    (Phase.INVENTORY_PENDING, "commit (ots path)"): _ERR,
    (Phase.INVENTORY_UPGRADED, "commit (ots path)"): Phase.COMMITTED,
    # reset / status: always legal, phase unchanged.
    **{
        (p, c): _STAY
        for c in ("reset", "status")
        for p in (
            Phase.DRAFTING,
            Phase.CONTENT_PENDING,
            Phase.CONTENT_UPGRADED,
            Phase.INVENTORY_PENDING,
            Phase.INVENTORY_UPGRADED,
        )
    },
}


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
