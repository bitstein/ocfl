"""Tests for Stage data structure, FixitySource protocol, and stage_dir."""

from typing import TYPE_CHECKING

from ocfl_lib.digest import DigestAlgorithm, file_digest, multi_digest

if TYPE_CHECKING:
    from pathlib import Path
from ocfl_lib.fs.local import LocalFS
from ocfl_lib.stage import FixitySource, Stage, stage_dir
from ocfl_lib.update_plan import ContentSource


class _DummyFS:
    def open_file(self, name):
        raise NotImplementedError

    def read_dir(self, name):
        raise NotImplementedError


class _DummyContentSource:
    """Minimal ContentSource for testing."""

    def __init__(self, files: dict[str, tuple[object, str]]):
        self._files = files

    def get_content(self, digest: str) -> tuple[object, str]:
        return self._files[digest]


class _DummyFixitySource:
    """Minimal FixitySource for testing."""

    def __init__(self, fixity: dict[str, dict[DigestAlgorithm, str]]):
        self._fixity = fixity

    def get_fixity(self, digest: str) -> dict[DigestAlgorithm, str]:
        return self._fixity.get(digest, {})


class TestFixitySourceProtocol:
    def test_dummy_satisfies_protocol(self):
        src = _DummyFixitySource({})
        assert isinstance(src, FixitySource)

    def test_get_fixity_returns_digest_map(self):
        src = _DummyFixitySource({"abc123": {DigestAlgorithm.SHA256: "def456"}})
        result = src.get_fixity("abc123")
        assert result == {DigestAlgorithm.SHA256: "def456"}

    def test_missing_digest_returns_empty(self):
        src = _DummyFixitySource({})
        assert src.get_fixity("missing") == {}


class TestStageConstruction:
    def test_basic_construction(self):
        state = {"digest1": ["file1.txt", "file2.txt"]}
        cs = _DummyContentSource({"digest1": (_DummyFS(), "path/to/file")})
        stage = Stage(
            state=state,
            digest_algorithm=DigestAlgorithm.SHA512,
            content_source=cs,
        )
        assert stage.state == state
        assert stage.digest_algorithm is DigestAlgorithm.SHA512
        assert stage.content_source is cs
        assert stage.fixity_source is None

    def test_construction_with_fixity(self):
        state = {"digest1": ["file.txt"]}
        cs = _DummyContentSource({"digest1": (_DummyFS(), "f")})
        fs_src = _DummyFixitySource({"digest1": {DigestAlgorithm.SHA256: "sha256hex"}})
        stage = Stage(
            state=state,
            digest_algorithm=DigestAlgorithm.SHA512,
            content_source=cs,
            fixity_source=fs_src,
        )
        assert stage.fixity_source is fs_src

    def test_content_source_satisfies_protocol(self):
        cs = _DummyContentSource({})
        assert isinstance(cs, ContentSource)

    def test_empty_state(self):
        stage = Stage(
            state={},
            digest_algorithm=DigestAlgorithm.SHA256,
            content_source=_DummyContentSource({}),
        )
        assert stage.state == {}

    def test_multiple_digests_in_state(self):
        state = {
            "aaa": ["a.txt"],
            "bbb": ["b.txt", "b_copy.txt"],
        }
        stage = Stage(
            state=state,
            digest_algorithm=DigestAlgorithm.SHA512,
            content_source=_DummyContentSource({}),
        )
        assert len(stage.state) == 2
        assert stage.state["bbb"] == ["b.txt", "b_copy.txt"]


class TestStageDir:
    def test_single_file(self, tmp_path: Path):
        (tmp_path / "hello.txt").write_text("hello world")
        fsys = LocalFS(tmp_path)
        stage = stage_dir(fsys, "", DigestAlgorithm.SHA512)

        assert stage.digest_algorithm is DigestAlgorithm.SHA512
        assert len(stage.state) == 1
        # Verify digest is correct
        expected = file_digest(tmp_path / "hello.txt", DigestAlgorithm.SHA512)
        assert expected in stage.state
        assert stage.state[expected] == ["hello.txt"]

    def test_nested_files(self, tmp_path: Path):
        (tmp_path / "a.txt").write_text("aaa")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.txt").write_text("bbb")

        fsys = LocalFS(tmp_path)
        stage = stage_dir(fsys, "", DigestAlgorithm.SHA256)

        # Should have 2 digests
        paths = {p for d, ps in stage.state.items() for p in ps}
        assert paths == {"a.txt", "sub/b.txt"}

    def test_duplicate_content(self, tmp_path: Path):
        """Files with same content share a digest entry."""
        (tmp_path / "orig.txt").write_text("same content")
        (tmp_path / "copy.txt").write_text("same content")

        fsys = LocalFS(tmp_path)
        stage = stage_dir(fsys, "", DigestAlgorithm.SHA256)

        # One digest with two paths
        assert len(stage.state) == 1
        digest = next(iter(stage.state))
        assert sorted(stage.state[digest]) == ["copy.txt", "orig.txt"]

    def test_content_source_resolves(self, tmp_path: Path):
        """ContentSource from stage_dir can retrieve files."""
        (tmp_path / "data.bin").write_bytes(b"\x00\x01\x02")

        fsys = LocalFS(tmp_path)
        stage = stage_dir(fsys, "", DigestAlgorithm.SHA512)
        digest = next(iter(stage.state))

        src_fs, src_path = stage.content_source.get_content(digest)
        with src_fs.open_file(src_path) as f:
            assert f.read() == b"\x00\x01\x02"

    def test_subdirectory_stage(self, tmp_path: Path):
        """Stage a subdirectory, not root."""
        sub = tmp_path / "payload"
        sub.mkdir()
        (sub / "file.txt").write_text("payload")

        fsys = LocalFS(tmp_path)
        stage = stage_dir(fsys, "payload", DigestAlgorithm.SHA256)

        paths = {p for d, ps in stage.state.items() for p in ps}
        assert paths == {"file.txt"}

    def test_empty_directory(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        stage = stage_dir(fsys, "", DigestAlgorithm.SHA512)
        assert stage.state == {}

    def test_fixity_algorithms(self, tmp_path: Path):
        """stage_dir with fixity_algs computes additional digests."""
        (tmp_path / "f.txt").write_text("fixity test")

        fsys = LocalFS(tmp_path)
        stage = stage_dir(
            fsys,
            "",
            DigestAlgorithm.SHA512,
            fixity_algs=[DigestAlgorithm.SHA256, DigestAlgorithm.MD5],
        )

        assert stage.fixity_source is not None
        digest = next(iter(stage.state))
        fixity = stage.fixity_source.get_fixity(digest)
        assert DigestAlgorithm.SHA256 in fixity
        assert DigestAlgorithm.MD5 in fixity
        # Verify fixity digests are correct
        expected = multi_digest(
            tmp_path / "f.txt", [DigestAlgorithm.SHA256, DigestAlgorithm.MD5]
        )
        assert fixity[DigestAlgorithm.SHA256] == expected[DigestAlgorithm.SHA256]
        assert fixity[DigestAlgorithm.MD5] == expected[DigestAlgorithm.MD5]

    def test_no_fixity_by_default(self, tmp_path: Path):
        (tmp_path / "f.txt").write_text("no fixity")
        fsys = LocalFS(tmp_path)
        stage = stage_dir(fsys, "", DigestAlgorithm.SHA512)
        assert stage.fixity_source is None
