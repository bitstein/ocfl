"""Tests for DigestAlgorithm."""

import hashlib

import pytest

from ocfl_lib.digest import DigestAlgorithm


class TestFromOcflName:
    @pytest.mark.parametrize(
        ("name", "member"),
        [
            ("sha512", DigestAlgorithm.SHA512),
            ("sha256", DigestAlgorithm.SHA256),
            ("sha1", DigestAlgorithm.SHA1),
            ("md5", DigestAlgorithm.MD5),
            ("blake2b-512", DigestAlgorithm.BLAKE2B_512),
            ("blake2b-256", DigestAlgorithm.BLAKE2B_256),
            ("blake2b-160", DigestAlgorithm.BLAKE2B_160),
            ("sha512/256", DigestAlgorithm.SHA512_256),
        ],
    )
    def test_round_trip(self, name: str, member: DigestAlgorithm):
        assert DigestAlgorithm.from_ocfl_name(name) is member
        assert member.ocfl_name == name

    def test_unknown_raises(self):
        with pytest.raises(ValueError):
            DigestAlgorithm.from_ocfl_name("sha384")


class TestIsSpecAlgorithm:
    @pytest.mark.parametrize("alg", [DigestAlgorithm.SHA512, DigestAlgorithm.SHA256])
    def test_spec_algorithms(self, alg: DigestAlgorithm):
        assert alg.is_spec_algorithm is True

    @pytest.mark.parametrize(
        "alg",
        [
            DigestAlgorithm.SHA1,
            DigestAlgorithm.MD5,
            DigestAlgorithm.BLAKE2B_512,
            DigestAlgorithm.BLAKE2B_256,
            DigestAlgorithm.BLAKE2B_160,
            DigestAlgorithm.SHA512_256,
        ],
    )
    def test_non_spec_algorithms(self, alg: DigestAlgorithm):
        assert alg.is_spec_algorithm is False


class TestRegex:
    def test_sha512_valid(self):
        assert DigestAlgorithm.SHA512.regex.match("a" * 128)

    def test_sha512_wrong_length(self):
        assert not DigestAlgorithm.SHA512.regex.match("a" * 127)

    def test_sha256_valid(self):
        assert DigestAlgorithm.SHA256.regex.match("abcdef0123456789" * 4)

    def test_rejects_non_hex(self):
        assert not DigestAlgorithm.SHA256.regex.match("g" * 64)

    @pytest.mark.parametrize("alg", list(DigestAlgorithm))
    def test_correct_length(self, alg: DigestAlgorithm):
        assert alg.regex.match("a" * alg.hex_length)
        assert not alg.regex.match("a" * (alg.hex_length + 1))
        assert not alg.regex.match("a" * (alg.hex_length - 1))


# Known test vectors: digest of empty bytes b""
EMPTY_DIGESTS = {
    DigestAlgorithm.SHA512: hashlib.sha512(b"").hexdigest(),
    DigestAlgorithm.SHA256: hashlib.sha256(b"").hexdigest(),
    DigestAlgorithm.SHA1: hashlib.sha1(b"").hexdigest(),
    DigestAlgorithm.MD5: hashlib.md5(b"").hexdigest(),
    DigestAlgorithm.BLAKE2B_512: hashlib.blake2b(b"", digest_size=64).hexdigest(),
    DigestAlgorithm.BLAKE2B_256: hashlib.blake2b(b"", digest_size=32).hexdigest(),
    DigestAlgorithm.BLAKE2B_160: hashlib.blake2b(b"", digest_size=20).hexdigest(),
}


class TestNewHasher:
    @pytest.mark.parametrize("alg", list(EMPTY_DIGESTS))
    def test_empty_digest(self, alg: DigestAlgorithm):
        h = alg.new_hasher()
        assert h.hexdigest() == EMPTY_DIGESTS[alg]

    @pytest.mark.parametrize("alg", list(DigestAlgorithm))
    def test_digest_length(self, alg: DigestAlgorithm):
        h = alg.new_hasher()
        h.update(b"hello")
        assert len(h.hexdigest()) == alg.hex_length

    def test_sha512_256(self):
        h = DigestAlgorithm.SHA512_256.new_hasher()
        h.update(b"")
        digest = h.hexdigest()
        assert len(digest) == 64
        assert DigestAlgorithm.SHA512_256.regex.match(digest)
