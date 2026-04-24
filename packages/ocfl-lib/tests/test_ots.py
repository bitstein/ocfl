"""Tests for ``ocfl_lib.ots`` parsing."""

import hashlib

import pytest

from ocfl_lib import ots
from ocfl_lib.errors import OtsParseError
from tests.helpers.ots import build_attestation


def test_parse_bitcoin_anchored_returns_block_height_and_attested_digest() -> None:
    file_digest = hashlib.sha256(b"bitcoin-anchored payload").digest()
    expected_height = 800_000

    raw = build_attestation(file_digest, block_height=expected_height)

    parsed = ots.parse(raw)

    assert parsed.commitment_digest == file_digest
    assert parsed.block_height == expected_height
    assert parsed.attested_digest == file_digest


def test_parse_pending_only_returns_none_for_block_and_attested_digest() -> None:
    file_digest = hashlib.sha256(b"pending payload").digest()

    raw = build_attestation(
        file_digest, pending_uri="https://a.pool.opentimestamps.org"
    )

    parsed = ots.parse(raw)

    assert parsed.commitment_digest == file_digest
    assert parsed.block_height is None
    assert parsed.attested_digest is None


def test_parse_garbage_bytes_raises_ots_parse_error() -> None:
    with pytest.raises(OtsParseError):
        ots.parse(b"not an ots")


def test_parse_truncated_valid_ots_raises_ots_parse_error() -> None:
    file_digest = hashlib.sha256(b"truncated payload").digest()
    raw = build_attestation(file_digest, block_height=750_000)

    truncated = raw[: len(raw) // 2]

    with pytest.raises(OtsParseError):
        ots.parse(truncated)


def test_build_attestation_requires_at_least_one_attestation() -> None:
    file_digest = hashlib.sha256(b"empty").digest()

    with pytest.raises(ValueError):
        build_attestation(file_digest)
