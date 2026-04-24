"""Layer-2 tests for ``BitcoinZmqSubscriber``.

Uses real pyzmq with an in-process PAIR socket pair: the test holds one PAIR
end and pushes a synthetic ``hashblock`` multipart frame to the subscriber's
PAIR end. Bitcoin Core publishes ``hashblock`` as ``[topic, hash, sequence]``
on PUB; our ``recv_multipart`` -> ``msg[1]`` logic is identical for PAIR.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import zmq

from ocfl_cli.bitcoin import BitcoinZmqSubscriber


@pytest.fixture
def zmq_pair() -> Iterator[tuple[zmq.Socket, BitcoinZmqSubscriber]]:
    """Yield (sender PAIR socket, subscriber wrapping the receiving PAIR)."""
    ctx = zmq.Context.instance()
    endpoint = "inproc://btc-zmq-test"

    sender = ctx.socket(zmq.PAIR)
    sender.bind(endpoint)

    receiver = ctx.socket(zmq.PAIR)
    receiver.connect(endpoint)

    sub = BitcoinZmqSubscriber(endpoint, ctx=ctx, socket=receiver)
    try:
        yield sender, sub
    finally:
        sub.close()
        sender.close(linger=0)


class TestWaitForBlock:
    def test_returns_block_hash_bytes_on_delivery(
        self, zmq_pair: tuple[zmq.Socket, BitcoinZmqSubscriber]
    ) -> None:
        sender, sub = zmq_pair
        block_hash = bytes.fromhex("ab" * 32)
        sequence = (1).to_bytes(4, "little")

        sender.send_multipart([b"hashblock", block_hash, sequence])

        got = sub.wait_for_block(timeout_ms=1000)

        assert got == block_hash

    def test_returns_none_on_timeout(
        self, zmq_pair: tuple[zmq.Socket, BitcoinZmqSubscriber]
    ) -> None:
        _sender, sub = zmq_pair

        got = sub.wait_for_block(timeout_ms=50)

        assert got is None

    def test_consecutive_blocks_drained_in_order(
        self, zmq_pair: tuple[zmq.Socket, BitcoinZmqSubscriber]
    ) -> None:
        sender, sub = zmq_pair
        h1 = bytes.fromhex("11" * 32)
        h2 = bytes.fromhex("22" * 32)
        sender.send_multipart([b"hashblock", h1, (1).to_bytes(4, "little")])
        sender.send_multipart([b"hashblock", h2, (2).to_bytes(4, "little")])

        got1 = sub.wait_for_block(timeout_ms=1000)
        got2 = sub.wait_for_block(timeout_ms=1000)
        got3 = sub.wait_for_block(timeout_ms=50)

        assert got1 == h1
        assert got2 == h2
        assert got3 is None
