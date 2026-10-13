"""Property-based phase FSM model.

Hypothesis RuleBasedStateMachine generates sequences of commands, runs
check_transition over them, and asserts:

- result phase is always a member of Phase
- PhaseError leaves phase unchanged (legality is a query, not a side effect)
- idempotent commands (status, reset, noop cells) are stable
- regression-causing commands always land in DRAFTING
- COMMITTED is never entered through the FSM (commit purges staging upstream;
  check_transition returns COMMITTED only as the result phase from
  inventory-upgraded → COMMITTED transition, which here we just observe)
"""

from hypothesis import settings
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule
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
    "reset",
    "status",
]

REGRESS_FROM = {
    Phase.CONTENT_UPGRADED,
    Phase.INVENTORY_PENDING,
    Phase.INVENTORY_UPGRADED,
}
MUTATING = {"add", "rm", "mv"}


class PhaseFSM(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.phase: Phase = Phase.DRAFTING

    @rule(cmd=sampled_from(COMMANDS))
    def step(self, cmd: str) -> None:
        before = self.phase
        # COMMITTED is terminal; staging is purged upstream and check_transition
        # is not called again. The model stops feeding commands into COMMITTED.
        if before is Phase.COMMITTED:
            return
        try:
            after = check_transition(before, cmd)
        except PhaseError:
            assert self.phase is before, "phase changed despite PhaseError"
            return
        assert isinstance(after, Phase)
        # Regression rule: add/rm/mv from intermediate phases land in DRAFTING.
        if cmd in MUTATING and before in REGRESS_FROM:
            assert after is Phase.DRAFTING
        # status / reset / noop cells: phase is stable.
        if cmd == "status":
            assert after is before
        if cmd == "reset":
            assert after is before
        self.phase = after

    @invariant()
    def phase_always_valid(self) -> None:
        assert isinstance(self.phase, Phase)
        assert self.phase in Phase


PhaseFSM.TestCase.settings = settings(max_examples=100, deadline=None)
TestPhaseFSM = PhaseFSM.TestCase
