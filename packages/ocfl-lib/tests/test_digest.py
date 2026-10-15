import hashlib
import io
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from ocfl_lib._digest import (
    _CHUNK_SIZE,
    DigestAlgorithm,
    bytes_digest,
    file_digest,
    stream_digest,
    stream_multi_digest,
)


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


HELLO_WORLD = b"hello world"
HELLO_SHA256 = hashlib.sha256(HELLO_WORLD).hexdigest()
HELLO_SHA512 = hashlib.sha512(HELLO_WORLD).hexdigest()


class TestFileDigest:
    def test_sha256(self, tmp_path: Path):
        f = tmp_path / "test.txt"
        f.write_bytes(HELLO_WORLD)
        assert file_digest(f, DigestAlgorithm.SHA256) == HELLO_SHA256

    def test_sha512(self, tmp_path: Path):
        f = tmp_path / "test.txt"
        f.write_bytes(HELLO_WORLD)
        assert file_digest(f, DigestAlgorithm.SHA512) == HELLO_SHA512

    def test_empty_file(self, tmp_path: Path):
        f = tmp_path / "empty.txt"
        f.write_bytes(b"")
        assert (
            file_digest(f, DigestAlgorithm.SHA256)
            == EMPTY_DIGESTS[DigestAlgorithm.SHA256]
        )

    @pytest.mark.parametrize("alg", list(DigestAlgorithm))
    def test_matches_bytes_digest_for_all_algorithms(
        self, tmp_path: Path, alg: DigestAlgorithm
    ):
        data = b"digest algorithm coverage"
        f = tmp_path / "test.bin"
        f.write_bytes(data)

        assert file_digest(f, alg) == bytes_digest(data, alg)


class TestStreamDigest:
    def test_sha256(self):
        stream = io.BytesIO(HELLO_WORLD)
        assert stream_digest(stream, DigestAlgorithm.SHA256) == HELLO_SHA256

    def test_empty_stream(self):
        stream = io.BytesIO(b"")
        assert (
            stream_digest(stream, DigestAlgorithm.SHA256)
            == EMPTY_DIGESTS[DigestAlgorithm.SHA256]
        )

    def test_large_stream(self):
        data = b"y" * (3 * _CHUNK_SIZE + 1)
        stream = io.BytesIO(data)
        assert (
            stream_digest(stream, DigestAlgorithm.SHA256)
            == hashlib.sha256(data).hexdigest()
        )


class TestBytesDigest:
    def test_sha256(self):
        assert bytes_digest(HELLO_WORLD, DigestAlgorithm.SHA256) == HELLO_SHA256

    def test_empty(self):
        assert (
            bytes_digest(b"", DigestAlgorithm.SHA512)
            == EMPTY_DIGESTS[DigestAlgorithm.SHA512]
        )

    @pytest.mark.parametrize("alg", list(EMPTY_DIGESTS))
    def test_all_algorithms(self, alg: DigestAlgorithm):
        assert bytes_digest(b"", alg) == EMPTY_DIGESTS[alg]


class TestStreamMultiDigest:
    def test_two_algorithms(self):
        result = stream_multi_digest(
            io.BytesIO(HELLO_WORLD), [DigestAlgorithm.SHA256, DigestAlgorithm.SHA512]
        )
        assert result[DigestAlgorithm.SHA256] == HELLO_SHA256
        assert result[DigestAlgorithm.SHA512] == HELLO_SHA512

    def test_matches_individual(self):
        data = b"test data for multi digest"
        algs = [DigestAlgorithm.SHA512, DigestAlgorithm.SHA256, DigestAlgorithm.MD5]
        result = stream_multi_digest(io.BytesIO(data), algs)
        for alg in algs:
            assert result[alg] == bytes_digest(data, alg)

    def test_empty_stream(self):
        result = stream_multi_digest(
            io.BytesIO(b""), [DigestAlgorithm.SHA256, DigestAlgorithm.SHA512]
        )
        assert result[DigestAlgorithm.SHA256] == EMPTY_DIGESTS[DigestAlgorithm.SHA256]
        assert result[DigestAlgorithm.SHA512] == EMPTY_DIGESTS[DigestAlgorithm.SHA512]

    def test_large_stream(self):
        data = b"z" * (3 * _CHUNK_SIZE + 1)
        result = stream_multi_digest(
            io.BytesIO(data), [DigestAlgorithm.SHA256, DigestAlgorithm.MD5]
        )
        assert result[DigestAlgorithm.SHA256] == hashlib.sha256(data).hexdigest()
        assert result[DigestAlgorithm.MD5] == hashlib.md5(data).hexdigest()
