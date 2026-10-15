from hypothesis import settings
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule
from hypothesis.strategies import sampled_from

from ocfl_cli.errors import PhaseError
from ocfl_cli.model.phase import Phase, PhaseCommand, check_transition

REGRESS_FROM = {
    Phase.CONTENT_UPGRADED,
    Phase.INVENTORY_PENDING,
    Phase.INVENTORY_UPGRADED,
}
MUTATING = {PhaseCommand.ADD, PhaseCommand.RM, PhaseCommand.MV}
TERMINAL = Phase.COMMITTED


class PhaseFSM(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.phase: Phase = Phase.DRAFTING

    @precondition(lambda self: self.phase is not TERMINAL)
    @rule(cmd=sampled_from(list(PhaseCommand)))
    def step(self, cmd: PhaseCommand) -> None:
        before = self.phase
        try:
            after = check_transition(before, cmd)
        except PhaseError:
            assert self.phase is before, "phase changed despite PhaseError"
            return
        assert isinstance(after, Phase)
        if cmd in MUTATING and before in REGRESS_FROM:
            assert after is Phase.DRAFTING
        if cmd is PhaseCommand.STATUS:
            assert after is before
        if cmd is PhaseCommand.DISCARD:
            assert after is before
        self.phase = after

    @invariant()
    def phase_always_valid(self) -> None:
        assert isinstance(self.phase, Phase)
        assert self.phase in Phase


PhaseFSM.TestCase.settings = settings(max_examples=100, deadline=None)
TestPhaseFSM = PhaseFSM.TestCase
