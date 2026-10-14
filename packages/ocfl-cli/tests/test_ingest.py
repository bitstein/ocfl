import dataclasses
import hashlib
from pathlib import Path

import pytest

from ocfl_cli.model.content import ContentMapEntry, IngestResult
from ocfl_cli.store.ingest import ingest_file
from ocfl_lib import DigestAlgorithm


@pytest.fixture
def staging_dir(tmp_path: Path) -> Path:
    d = tmp_path / "staging"
    d.mkdir()
    return d


def _write(src: Path, data: bytes) -> Path:
    src.write_bytes(data)
    return src


class TestNewDigest:
    def test_new_digest_records_result_and_entry(self, tmp_path, staging_dir):
        src = _write(tmp_path / "hello.txt", b"hello")
        content_map: dict[str, ContentMapEntry] = {}

        result = ingest_file(
            src,
            "hello.txt",
            staging_dir=staging_dir,
            content_map=content_map,
            digest_alg=DigestAlgorithm.SHA256,
        )

        expected_digest = hashlib.sha256(b"hello").hexdigest()
        assert result.already_existed is False
        assert result.digest == expected_digest
        assert result.size == len(b"hello")

        entry = content_map[expected_digest]
        assert entry["path"] == str(src)
        assert Path(entry["path"]).is_absolute()
        assert entry["size"] == len(b"hello")
        assert entry["mtime"] == src.stat().st_mtime


class TestIdempotentSamePath:
    def test_same_file_twice_returns_already_existed(self, tmp_path, staging_dir):
        src = _write(tmp_path / "hello.txt", b"hello")
        content_map: dict[str, ContentMapEntry] = {}

        first = ingest_file(
            src,
            "hello.txt",
            staging_dir=staging_dir,
            content_map=content_map,
            digest_alg=DigestAlgorithm.SHA256,
        )
        snapshot = dict(content_map[first.digest])

        second = ingest_file(
            src,
            "hello.txt",
            staging_dir=staging_dir,
            content_map=content_map,
            digest_alg=DigestAlgorithm.SHA256,
        )

        assert second.already_existed is True
        assert second.digest == first.digest
        assert len(content_map) == 1
        assert content_map[first.digest] == snapshot


class TestDedupSameBytesDifferentObjRelPath:
    def test_second_path_dedups_to_existing_digest(self, tmp_path, staging_dir):
        src_a = _write(tmp_path / "a.txt", b"hello")
        src_b = _write(tmp_path / "b.txt", b"hello")
        content_map: dict[str, ContentMapEntry] = {}

        first = ingest_file(
            src_a,
            "a.txt",
            staging_dir=staging_dir,
            content_map=content_map,
            digest_alg=DigestAlgorithm.SHA256,
        )
        second = ingest_file(
            src_b,
            "b.txt",
            staging_dir=staging_dir,
            content_map=content_map,
            digest_alg=DigestAlgorithm.SHA256,
        )

        assert first.digest == second.digest
        assert second.already_existed is True
        assert len(content_map) == 1


class TestRecordsSizeAndMtime:
    def test_size_and_mtime_from_stat(self, tmp_path, staging_dir):
        src = _write(tmp_path / "f.bin", b"payload-bytes")
        content_map: dict[str, ContentMapEntry] = {}

        result = ingest_file(
            src,
            "f.bin",
            staging_dir=staging_dir,
            content_map=content_map,
            digest_alg=DigestAlgorithm.SHA256,
        )

        st = src.stat()
        entry = content_map[result.digest]
        assert entry["size"] == st.st_size
        assert entry["mtime"] == st.st_mtime
        assert result.size == st.st_size


class TestReturnsIngestResultShape:
    def test_is_frozen_and_slotted(self, tmp_path, staging_dir):
        src = _write(tmp_path / "x.txt", b"x")
        content_map: dict[str, ContentMapEntry] = {}

        result = ingest_file(
            src,
            "x.txt",
            staging_dir=staging_dir,
            content_map=content_map,
            digest_alg=DigestAlgorithm.SHA256,
        )

        assert isinstance(result, IngestResult)
        assert dataclasses.is_dataclass(result)
        assert {f.name for f in dataclasses.fields(result)} == {
            "digest",
            "size",
            "already_existed",
        }
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(result, "digest", "mutated")  # noqa: B010
        assert not hasattr(result, "__dict__")


class TestMutatesContentMapInPlace:
    def test_same_dict_object_is_mutated(self, tmp_path, staging_dir):
        src = _write(tmp_path / "x.txt", b"x")
        content_map: dict[str, ContentMapEntry] = {}
        sentinel_id = id(content_map)

        ingest_file(
            src,
            "x.txt",
            staging_dir=staging_dir,
            content_map=content_map,
            digest_alg=DigestAlgorithm.SHA256,
        )

        assert id(content_map) == sentinel_id
        assert len(content_map) == 1
