"""In-memory fakes for tests.

Mirror the `SimpleProofClient`, `BitcoinRPC`, and `HashBlockStream`
protocols with injectable fault flags.
"""

from .fake_bitcoin import FakeBitcoinRPC, InMemoryHashBlockStream
from .fake_simpleproof import FakeSimpleProof

__all__ = [
    "FakeBitcoinRPC",
    "FakeSimpleProof",
    "InMemoryHashBlockStream",
]
