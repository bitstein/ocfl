import hashlib
import io
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from ocfl_lib._digest import (
    DigestAlgorithm,
    bytes_digest,
    digest_files,
    digest_files_multi,
    file_digest,
    multi_digest,
    stream_digest,
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
        data = b"y" * 100_000
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


class TestMultiDigest:
    def test_two_algorithms(self, tmp_path: Path):
        f = tmp_path / "test.txt"
        f.write_bytes(HELLO_WORLD)
        result = multi_digest(f, [DigestAlgorithm.SHA256, DigestAlgorithm.SHA512])
        assert result[DigestAlgorithm.SHA256] == HELLO_SHA256
        assert result[DigestAlgorithm.SHA512] == HELLO_SHA512

    def test_matches_individual(self, tmp_path: Path):
        data = b"test data for multi digest"
        f = tmp_path / "test.bin"
        f.write_bytes(data)
        algs = [DigestAlgorithm.SHA512, DigestAlgorithm.SHA256, DigestAlgorithm.MD5]
        result = multi_digest(f, algs)
        for alg in algs:
            assert result[alg] == file_digest(f, alg)

    def test_single_algorithm(self, tmp_path: Path):
        f = tmp_path / "test.txt"
        f.write_bytes(HELLO_WORLD)
        result = multi_digest(f, [DigestAlgorithm.SHA256])
        assert result[DigestAlgorithm.SHA256] == HELLO_SHA256

    def test_empty_file(self, tmp_path: Path):
        f = tmp_path / "empty.txt"
        f.write_bytes(b"")
        result = multi_digest(f, [DigestAlgorithm.SHA256, DigestAlgorithm.SHA512])
        assert result[DigestAlgorithm.SHA256] == EMPTY_DIGESTS[DigestAlgorithm.SHA256]
        assert result[DigestAlgorithm.SHA512] == EMPTY_DIGESTS[DigestAlgorithm.SHA512]

    def test_large_file(self, tmp_path: Path):
        """Verify multi-pass works across chunk boundaries."""
        data = b"z" * 100_000
        f = tmp_path / "large.bin"
        f.write_bytes(data)
        result = multi_digest(f, [DigestAlgorithm.SHA256, DigestAlgorithm.MD5])
        assert result[DigestAlgorithm.SHA256] == hashlib.sha256(data).hexdigest()
        assert result[DigestAlgorithm.MD5] == hashlib.md5(data).hexdigest()


def _make_files(tmp_path: Path, count: int = 5) -> list[Path]:
    paths = []
    for i in range(count):
        f = tmp_path / f"file_{i}.bin"
        f.write_bytes(f"content {i}".encode())
        paths.append(f)
    return paths


class TestDigestFiles:
    def test_matches_serial(self, tmp_path: Path):
        paths = _make_files(tmp_path)
        result = digest_files(paths, DigestAlgorithm.SHA256)
        for p in paths:
            assert result[p] == file_digest(p, DigestAlgorithm.SHA256)

    def test_single_file(self, tmp_path: Path):
        f = tmp_path / "single.txt"
        f.write_bytes(HELLO_WORLD)
        result = digest_files([f], DigestAlgorithm.SHA256)
        assert result[f] == HELLO_SHA256

    def test_empty_list(self):
        result = digest_files([], DigestAlgorithm.SHA256)
        assert result == {}

    def test_max_workers(self, tmp_path: Path):
        paths = _make_files(tmp_path)
        result = digest_files(paths, DigestAlgorithm.SHA256, max_workers=2)
        for p in paths:
            assert result[p] == file_digest(p, DigestAlgorithm.SHA256)


class TestDigestFilesMulti:
    def test_matches_serial(self, tmp_path: Path):
        paths = _make_files(tmp_path)
        algs = [DigestAlgorithm.SHA256, DigestAlgorithm.MD5]
        result = digest_files_multi(paths, algs)
        for p in paths:
            for alg in algs:
                assert result[p][alg] == file_digest(p, alg)

    def test_single_algorithm(self, tmp_path: Path):
        paths = _make_files(tmp_path, count=3)
        result = digest_files_multi(paths, [DigestAlgorithm.SHA512])
        for p in paths:
            assert result[p][DigestAlgorithm.SHA512] == file_digest(
                p, DigestAlgorithm.SHA512
            )

    def test_empty_list(self):
        result = digest_files_multi([], [DigestAlgorithm.SHA256])
        assert result == {}


class TestDigestFilesErrors:
    def test_two_missing_files_raises_exception_group(self, tmp_path: Path):
        missing1 = tmp_path / "nope1"
        missing2 = tmp_path / "nope2"
        with pytest.raises(ExceptionGroup) as exc_info:
            digest_files([missing1, missing2], DigestAlgorithm.SHA256)
        assert len(exc_info.value.exceptions) == 2
        assert all(isinstance(e, FileNotFoundError) for e in exc_info.value.exceptions)
        for exc in exc_info.value.exceptions:
            assert any("while digesting" in n for n in exc.__notes__)

    def test_single_missing_file_raises_exception_group(self, tmp_path: Path):
        missing = tmp_path / "nope"
        with pytest.raises(ExceptionGroup):
            digest_files([missing], DigestAlgorithm.SHA256)

    def test_mixed_good_and_bad_raises_exception_group(self, tmp_path: Path):
        good = tmp_path / "good.txt"
        good.write_bytes(b"ok")
        missing = tmp_path / "nope"
        with pytest.raises(ExceptionGroup) as exc_info:
            digest_files([good, missing], DigestAlgorithm.SHA256)
        assert len(exc_info.value.exceptions) == 1

    def test_multi_two_missing_raises_exception_group(self, tmp_path: Path):
        missing1 = tmp_path / "nope1"
        missing2 = tmp_path / "nope2"
        algs = [DigestAlgorithm.SHA256, DigestAlgorithm.SHA512]
        with pytest.raises(ExceptionGroup) as exc_info:
            digest_files_multi([missing1, missing2], algs)
        assert len(exc_info.value.exceptions) == 2
