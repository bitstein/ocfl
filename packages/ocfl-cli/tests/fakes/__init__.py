"""In-memory fakes for tests.

Mirror the ports (`SimpleProofClient`, `BitcoinRPC`, `BlockStream`, `Clock`,
`Sleeper`) with injectable fault flags. Inject them through `ctx.obj` under
the `AppCtx` names, or pass them in a `Runtime`.
"""

from .fake_bitcoin import FakeBitcoinRPC, InMemoryHashBlockStream
from .fake_simpleproof import FakeSimpleProof
from .fake_time import FakeClock, RecordingSleeper

__all__ = [
    "FakeBitcoinRPC",
    "FakeClock",
    "FakeSimpleProof",
    "InMemoryHashBlockStream",
    "RecordingSleeper",
]
