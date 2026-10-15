"""Uses real pyzmq with an in-process PAIR socket pair: the test holds one PAIR
end and pushes a synthetic ``hashblock`` multipart frame to the subscriber's
PAIR end. Bitcoin Core publishes ``hashblock`` as ``[topic, hash, sequence]``
on PUB; our ``recv_multipart`` -> ``msg[1]`` logic is identical for PAIR.
"""

import time
from collections.abc import Iterator
from typing import NamedTuple
from uuid import uuid4

import pytest
import zmq

from ocfl_cli.ots.bitcoin import BitcoinZmqSubscriber


class ZmqPair(NamedTuple):
    sender: zmq.Socket
    sub: BitcoinZmqSubscriber


@pytest.fixture
def zmq_pair() -> Iterator[ZmqPair]:
    ctx = zmq.Context()
    endpoint = f"inproc://btc-zmq-{uuid4().hex}"

    sender = ctx.socket(zmq.PAIR)
    sender.bind(endpoint)

    receiver = ctx.socket(zmq.PAIR)
    receiver.connect(endpoint)

    sub = BitcoinZmqSubscriber(endpoint, ctx=ctx, socket=receiver)
    try:
        yield ZmqPair(sender, sub)
    finally:
        sub.close()
        sender.close(linger=0)
        ctx.term()


class TestWaitForBlock:
    def test_constructed_sub_socket_receives_hashblock_topic(self) -> None:
        ctx = zmq.Context()
        endpoint = f"inproc://btc-zmq-{uuid4().hex}"
        sender = ctx.socket(zmq.PUB)
        sender.bind(endpoint)
        sub = BitcoinZmqSubscriber(endpoint, ctx=ctx)
        block_hash = bytes.fromhex("ab" * 32)
        decoy_hash = bytes.fromhex("cd" * 32)

        try:
            deadline = time.monotonic() + 1
            got = None
            while time.monotonic() < deadline:
                sender.send_multipart([b"rawtx", decoy_hash, (1).to_bytes(4, "little")])
                sender.send_multipart(
                    [b"hashblock", block_hash, (2).to_bytes(4, "little")]
                )
                got = sub.wait_for_block(timeout_ms=50)
                if got is not None:
                    break

            assert got == block_hash
        finally:
            sub.close()
            sender.close(linger=0)
            ctx.term()

    def test_returns_block_hash_bytes_on_delivery(self, zmq_pair: ZmqPair) -> None:
        sender, sub = zmq_pair
        block_hash = bytes.fromhex("ab" * 32)
        sequence = (1).to_bytes(4, "little")

        sender.send_multipart([b"hashblock", block_hash, sequence])

        got = sub.wait_for_block(timeout_ms=1000)

        assert got == block_hash

    def test_returns_none_on_timeout(self, zmq_pair: ZmqPair) -> None:
        _sender, sub = zmq_pair

        got = sub.wait_for_block(timeout_ms=50)

        assert got is None

    def test_returns_none_after_close(self, zmq_pair: ZmqPair) -> None:
        _sender, sub = zmq_pair
        sub.close()

        got = sub.wait_for_block(timeout_ms=50)

        assert got is None

    def test_short_multipart_frame_returns_none(self, zmq_pair: ZmqPair) -> None:
        sender, sub = zmq_pair
        sender.send_multipart([b"hashblock"])

        got = sub.wait_for_block(timeout_ms=1000)

        assert got is None

    def test_close_is_idempotent(self, zmq_pair: ZmqPair) -> None:
        _sender, sub = zmq_pair

        sub.close()
        sub.close()

        assert sub.wait_for_block(timeout_ms=50) is None

    def test_context_manager_closes_subscriber(self) -> None:
        ctx = zmq.Context()
        endpoint = f"inproc://btc-zmq-{uuid4().hex}"
        receiver = ctx.socket(zmq.PAIR)

        try:
            with BitcoinZmqSubscriber(endpoint, ctx=ctx, socket=receiver) as sub:
                assert sub.wait_for_block(timeout_ms=50) is None

            assert sub.wait_for_block(timeout_ms=50) is None
        finally:
            sub.close()
            ctx.term()

    def test_consecutive_blocks_drained_in_order(self, zmq_pair: ZmqPair) -> None:
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
