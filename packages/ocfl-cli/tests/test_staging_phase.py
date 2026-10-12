"""Phase StrEnum + check_transition matrix.

Mirrors workflow yaml `architecture.transitions.matrix`. Every (phase, command)
cell is pinned here; check_transition is a pure function of (phase, command)
and returns the resulting phase or raises PhaseError.

Outcomes per spec:
- stay:        returns same phase
- advance-X:   returns new phase X
- regress-...: returns Phase.DRAFTING (state-mutation upstream)
- noop:        returns same phase (idempotent)
- err:         raises PhaseError
- reset/status: always succeed (return same phase)

Matrix (rows = command; cols = D/CP/CU/IP/IU; cells = stay|advance-X|regress|noop|err):

| command                          |  D  | CP  | CU  | IP  | IU  |
| -------------------------------- | --- | --- | --- | --- | --- |
| add                              |stay |stay | reg | reg | reg |
| rm                               |stay |stay | reg | reg | reg |
| mv                               |stay |stay | reg | reg | reg |
| timestamp content                | CP  |stay |noop | err | err |
| timestamp content --upgrade      | err |stay |noop | err | err |
| timestamp content --upgrade --w  | err |stay |noop | err | err |
| timestamp content --retry        | err |stay | err | err | err |
| timestamp inventory              | err | err | IP  |stay |noop |
| timestamp inventory --upgrade    | err | err | err | IU  |noop |
| timestamp inventory --upgrade --w| err | err | err | IU  |noop |
| timestamp inventory --retry      | err | err | err |stay | err |
| commit (ots path)                | err | err | err | err |COMM |
| reset                            |stay |stay |stay |stay |stay |
| status                           |stay |stay |stay |stay |stay |

The "stay-or-advance" cells (timestamp content --upgrade at content-pending)
collapse to "stay": the actual advance is upstream, after verify completes.
The "advance-content-pending-or-straight-to-upgraded-if-vacuous" cell
(timestamp content at drafting) collapses to advance-content-pending: the
vacuous fast-path is also an upstream concern.
"""

import pytest

from ocfl_cli.errors import PhaseError
from ocfl_cli.model.phase import Phase, check_transition

D = Phase.DRAFTING
CP = Phase.CONTENT_PENDING
CU = Phase.CONTENT_UPGRADED
IP = Phase.INVENTORY_PENDING
IU = Phase.INVENTORY_UPGRADED
COMM = Phase.COMMITTED

ERR = "err"  # sentinel for parametrize: command raises PhaseError

# (phase, command) -> Phase | "err"
MATRIX: dict[tuple[Phase, str], Phase | str] = {
    # add
    (D, "add"): D,
    (CP, "add"): CP,
    (CU, "add"): D,
    (IP, "add"): D,
    (IU, "add"): D,
    # rm
    (D, "rm"): D,
    (CP, "rm"): CP,
    (CU, "rm"): D,
    (IP, "rm"): D,
    (IU, "rm"): D,
    # mv
    (D, "mv"): D,
    (CP, "mv"): CP,
    (CU, "mv"): D,
    (IP, "mv"): D,
    (IU, "mv"): D,
    # timestamp content
    (D, "timestamp content"): CP,
    (CP, "timestamp content"): CP,
    (CU, "timestamp content"): CU,
    (IP, "timestamp content"): ERR,
    (IU, "timestamp content"): ERR,
    # timestamp content --upgrade
    (D, "timestamp content --upgrade"): ERR,
    (CP, "timestamp content --upgrade"): CP,
    (CU, "timestamp content --upgrade"): CU,
    (IP, "timestamp content --upgrade"): ERR,
    (IU, "timestamp content --upgrade"): ERR,
    # timestamp content --upgrade --watch
    (D, "timestamp content --upgrade --watch"): ERR,
    (CP, "timestamp content --upgrade --watch"): CP,
    (CU, "timestamp content --upgrade --watch"): CU,
    (IP, "timestamp content --upgrade --watch"): ERR,
    (IU, "timestamp content --upgrade --watch"): ERR,
    # timestamp content --retry
    (D, "timestamp content --retry"): ERR,
    (CP, "timestamp content --retry"): CP,
    (CU, "timestamp content --retry"): ERR,
    (IP, "timestamp content --retry"): ERR,
    (IU, "timestamp content --retry"): ERR,
    # timestamp inventory
    (D, "timestamp inventory"): ERR,
    (CP, "timestamp inventory"): ERR,
    (CU, "timestamp inventory"): IP,
    (IP, "timestamp inventory"): IP,
    (IU, "timestamp inventory"): IU,
    # timestamp inventory --upgrade
    (D, "timestamp inventory --upgrade"): ERR,
    (CP, "timestamp inventory --upgrade"): ERR,
    (CU, "timestamp inventory --upgrade"): ERR,
    (IP, "timestamp inventory --upgrade"): IU,
    (IU, "timestamp inventory --upgrade"): IU,
    # timestamp inventory --upgrade --watch
    (D, "timestamp inventory --upgrade --watch"): ERR,
    (CP, "timestamp inventory --upgrade --watch"): ERR,
    (CU, "timestamp inventory --upgrade --watch"): ERR,
    (IP, "timestamp inventory --upgrade --watch"): IU,
    (IU, "timestamp inventory --upgrade --watch"): IU,
    # timestamp inventory --retry
    (D, "timestamp inventory --retry"): ERR,
    (CP, "timestamp inventory --retry"): ERR,
    (CU, "timestamp inventory --retry"): ERR,
    (IP, "timestamp inventory --retry"): IP,
    (IU, "timestamp inventory --retry"): ERR,
    # commit (ots path)
    (D, "commit (ots path)"): ERR,
    (CP, "commit (ots path)"): ERR,
    (CU, "commit (ots path)"): ERR,
    (IP, "commit (ots path)"): ERR,
    (IU, "commit (ots path)"): COMM,
    # reset — always legal in any phase
    (D, "reset"): D,
    (CP, "reset"): CP,
    (CU, "reset"): CU,
    (IP, "reset"): IP,
    (IU, "reset"): IU,
    # status — always legal, read-only
    (D, "status"): D,
    (CP, "status"): CP,
    (CU, "status"): CU,
    (IP, "status"): IP,
    (IU, "status"): IU,
}


class TestPhaseEnum:
    def test_phase_is_str_enum(self):
        import enum

        assert issubclass(Phase, enum.StrEnum)

    @pytest.mark.parametrize(
        ("member", "wire"),
        [
            (Phase.DRAFTING, "drafting"),
            (Phase.CONTENT_PENDING, "content-pending"),
            (Phase.CONTENT_UPGRADED, "content-upgraded"),
            (Phase.INVENTORY_PENDING, "inventory-pending"),
            (Phase.INVENTORY_UPGRADED, "inventory-upgraded"),
            (Phase.COMMITTED, "committed"),
        ],
    )
    def test_wire_strings(self, member: Phase, wire: str):
        assert member.value == wire
        assert str(member) == wire

    def test_phase_count(self):
        assert len(list(Phase)) == 6

    def test_round_trip_from_string(self):
        for member in Phase:
            assert Phase(member.value) is member


@pytest.mark.parametrize(
    ("phase", "command", "expected"), [(p, c, e) for (p, c), e in MATRIX.items()]
)
def test_check_transition_matrix(phase: Phase, command: str, expected: Phase | str):
    if expected == ERR:
        with pytest.raises(PhaseError):
            check_transition(phase, command)
    else:
        result = check_transition(phase, command)
        assert result is expected
        assert isinstance(result, Phase)


def test_check_transition_unknown_command_raises():
    with pytest.raises(PhaseError):
        check_transition(Phase.DRAFTING, "frobnicate")


def test_matrix_covers_every_pre_committed_phase():
    """Every (non-committed phase, command) pair has a pinned outcome."""
    commands = {c for _, c in MATRIX}
    pre_committed = [D, CP, CU, IP, IU]
    for phase in pre_committed:
        for cmd in commands:
            assert (phase, cmd) in MATRIX, f"missing cell: ({phase}, {cmd})"
