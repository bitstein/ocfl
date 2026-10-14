from hypothesis import settings
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule
from hypothesis.strategies import sampled_from

from ocfl_cli.errors import PhaseError
from ocfl_cli.model.phase import Phase, check_transition

COMMANDS = [
    "add",
    "rm",
    "mv",
    "timestamp content",
    "upgrade content",
    "retry content",
    "timestamp inventory",
    "upgrade inventory",
    "retry inventory",
    "commit (ots path)",
    "discard",
    "status",
]

REGRESS_FROM = {
    Phase.CONTENT_UPGRADED,
    Phase.INVENTORY_PENDING,
    Phase.INVENTORY_UPGRADED,
}
MUTATING = {"add", "rm", "mv"}
TERMINAL = Phase.COMMITTED


class PhaseFSM(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.phase: Phase = Phase.DRAFTING

    @precondition(lambda self: self.phase is not TERMINAL)
    @rule(cmd=sampled_from(COMMANDS))
    def step(self, cmd: str) -> None:
        before = self.phase
        try:
            after = check_transition(before, cmd)
        except PhaseError:
            assert self.phase is before, "phase changed despite PhaseError"
            return
        assert isinstance(after, Phase)
        if cmd in MUTATING and before in REGRESS_FROM:
            assert after is Phase.DRAFTING
        if cmd == "status":
            assert after is before
        if cmd == "discard":
            assert after is before
        self.phase = after

    @invariant()
    def phase_always_valid(self) -> None:
        assert isinstance(self.phase, Phase)
        assert self.phase in Phase


PhaseFSM.TestCase.settings = settings(max_examples=100, deadline=None)
TestPhaseFSM = PhaseFSM.TestCase
