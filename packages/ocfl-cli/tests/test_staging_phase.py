import pytest

from ocfl_cli.errors import PhaseError
from ocfl_cli.model.phase import Phase, check_transition

D = Phase.DRAFTING
CP = Phase.CONTENT_PENDING
CU = Phase.CONTENT_UPGRADED
IP = Phase.INVENTORY_PENDING
IU = Phase.INVENTORY_UPGRADED
COMM = Phase.COMMITTED

RAISES_PHASE_ERROR = "raises PhaseError"

MATRIX: dict[tuple[Phase, str], Phase | str] = {
    (D, "add"): D,
    (CP, "add"): CP,
    (CU, "add"): D,
    (IP, "add"): D,
    (IU, "add"): D,
    (D, "rm"): D,
    (CP, "rm"): CP,
    (CU, "rm"): D,
    (IP, "rm"): D,
    (IU, "rm"): D,
    (D, "mv"): D,
    (CP, "mv"): CP,
    (CU, "mv"): D,
    (IP, "mv"): D,
    (IU, "mv"): D,
    (D, "timestamp content"): CP,
    (CP, "timestamp content"): CP,
    (CU, "timestamp content"): CU,
    (IP, "timestamp content"): RAISES_PHASE_ERROR,
    (IU, "timestamp content"): RAISES_PHASE_ERROR,
    (D, "upgrade content"): RAISES_PHASE_ERROR,
    (CP, "upgrade content"): CP,
    (CU, "upgrade content"): CU,
    (IP, "upgrade content"): RAISES_PHASE_ERROR,
    (IU, "upgrade content"): RAISES_PHASE_ERROR,
    (D, "retry content"): RAISES_PHASE_ERROR,
    (CP, "retry content"): CP,
    (CU, "retry content"): RAISES_PHASE_ERROR,
    (IP, "retry content"): RAISES_PHASE_ERROR,
    (IU, "retry content"): RAISES_PHASE_ERROR,
    (D, "timestamp inventory"): RAISES_PHASE_ERROR,
    (CP, "timestamp inventory"): RAISES_PHASE_ERROR,
    (CU, "timestamp inventory"): IP,
    (IP, "timestamp inventory"): IP,
    (IU, "timestamp inventory"): IU,
    (D, "upgrade inventory"): RAISES_PHASE_ERROR,
    (CP, "upgrade inventory"): RAISES_PHASE_ERROR,
    (CU, "upgrade inventory"): RAISES_PHASE_ERROR,
    (IP, "upgrade inventory"): IU,
    (IU, "upgrade inventory"): IU,
    (D, "retry inventory"): RAISES_PHASE_ERROR,
    (CP, "retry inventory"): RAISES_PHASE_ERROR,
    (CU, "retry inventory"): RAISES_PHASE_ERROR,
    (IP, "retry inventory"): IP,
    (IU, "retry inventory"): RAISES_PHASE_ERROR,
    (D, "commit (ots path)"): RAISES_PHASE_ERROR,
    (CP, "commit (ots path)"): RAISES_PHASE_ERROR,
    (CU, "commit (ots path)"): RAISES_PHASE_ERROR,
    (IP, "commit (ots path)"): RAISES_PHASE_ERROR,
    (IU, "commit (ots path)"): COMM,
    (D, "discard"): D,
    (CP, "discard"): CP,
    (CU, "discard"): CU,
    (IP, "discard"): IP,
    (IU, "discard"): IU,
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
    if expected == RAISES_PHASE_ERROR:
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
    commands = {c for _, c in MATRIX}
    pre_committed = [D, CP, CU, IP, IU]
    for phase in pre_committed:
        for cmd in commands:
            assert (phase, cmd) in MATRIX, f"missing cell: ({phase}, {cmd})"
