"""Tests for VersionState, FixitySource protocol, and scan_dir."""

from typing import TYPE_CHECKING

import pytest

from ocfl_lib._digest import DigestAlgorithm, file_digest, multi_digest
from ocfl_lib.fs._protocols import FS

if TYPE_CHECKING:
    from pathlib import Path
from ocfl_lib._object import Object
from ocfl_lib._sources import ContentSource
from ocfl_lib._spec import VNum
from ocfl_lib._version_state import FixitySource, VersionState, scan_dir
from ocfl_lib.fs._local import LocalFS


class _DummyFS:
    def open_file(self, name):
        raise NotImplementedError

    def read_dir(self, name):
        raise NotImplementedError

    def exists(self, name):
        return False


class _DummyContentSource(ContentSource):
    """Minimal ContentSource for testing."""

    def __init__(self, files: dict[str, tuple[FS, str]]):
        self._files = files

    def get_content(self, digest: str) -> tuple[FS, str]:
        return self._files[digest]


class _DummyFixitySource(FixitySource):
    """Minimal FixitySource for testing."""

    def __init__(self, fixity: dict[str, dict[DigestAlgorithm, str]]):
        self._fixity = fixity

    def get_fixity(self, digest: str) -> dict[DigestAlgorithm, str]:
        return self._fixity.get(digest, {})


class TestFixitySourceProtocol:
    def test_dummy_satisfies_protocol(self):
        src = _DummyFixitySource({})
        assert src is not None

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
        stage = VersionState(
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
        stage = VersionState(
            state=state,
            digest_algorithm=DigestAlgorithm.SHA512,
            content_source=cs,
            fixity_source=fs_src,
        )
        assert stage.fixity_source is fs_src

    def test_content_source_satisfies_protocol(self):
        cs = _DummyContentSource({})
        assert cs is not None

    def test_empty_state(self):
        stage = VersionState(
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
        stage = VersionState(
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
        stage = scan_dir(fsys, "", DigestAlgorithm.SHA512)

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
        stage = scan_dir(fsys, "", DigestAlgorithm.SHA256)

        # Should have 2 digests
        paths = {p for d, ps in stage.state.items() for p in ps}
        assert paths == {"a.txt", "sub/b.txt"}

    def test_duplicate_content(self, tmp_path: Path):
        """Files with same content share a digest entry."""
        (tmp_path / "orig.txt").write_text("same content")
        (tmp_path / "copy.txt").write_text("same content")

        fsys = LocalFS(tmp_path)
        stage = scan_dir(fsys, "", DigestAlgorithm.SHA256)

        # One digest with two paths
        assert len(stage.state) == 1
        digest = next(iter(stage.state))
        assert sorted(stage.state[digest]) == ["copy.txt", "orig.txt"]

    def test_content_source_resolves(self, tmp_path: Path):
        """ContentSource from scan_dir can retrieve files."""
        (tmp_path / "data.bin").write_bytes(b"\x00\x01\x02")

        fsys = LocalFS(tmp_path)
        stage = scan_dir(fsys, "", DigestAlgorithm.SHA512)
        digest = next(iter(stage.state))

        src_fs, src_path = stage.content_source.get_content(digest)
        with src_fs.open_file(src_path) as f:
            assert f.read() == b"\x00\x01\x02"

    def test_subdirectory_stage(self, tmp_path: Path):
        """Scan a subdirectory, not root."""
        sub = tmp_path / "payload"
        sub.mkdir()
        (sub / "file.txt").write_text("payload")

        fsys = LocalFS(tmp_path)
        stage = scan_dir(fsys, "payload", DigestAlgorithm.SHA256)

        paths = {p for d, ps in stage.state.items() for p in ps}
        assert paths == {"file.txt"}

    def test_empty_directory(self, tmp_path: Path):
        fsys = LocalFS(tmp_path)
        stage = scan_dir(fsys, "", DigestAlgorithm.SHA512)
        assert stage.state == {}

    def test_fixity_algorithms(self, tmp_path: Path):
        """scan_dir with fixity_algs computes additional digests."""
        (tmp_path / "f.txt").write_text("fixity test")

        fsys = LocalFS(tmp_path)
        stage = scan_dir(
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
        stage = scan_dir(fsys, "", DigestAlgorithm.SHA512)
        assert stage.fixity_source is None


def _make_stage(
    state: dict[str, list[str]],
    alg: DigestAlgorithm = DigestAlgorithm.SHA512,
    files: dict[str, tuple[FS, str]] | None = None,
    fixity: dict[str, dict[DigestAlgorithm, str]] | None = None,
) -> VersionState:
    """Helper to build a VersionState with dummy sources."""
    cs = _DummyContentSource(files or {})
    fs = _DummyFixitySource(fixity) if fixity else None
    return VersionState(
        state=state, digest_algorithm=alg, content_source=cs, fixity_source=fs
    )


class TestStageOverlay:
    def test_overlay_adds_new_files(self):
        base = _make_stage({"d1": ["a.txt"]})
        overlay = _make_stage({"d2": ["b.txt"]})
        merged = base.overlay(overlay)
        assert "d1" in merged.state
        assert "d2" in merged.state

    def test_overlay_replaces_by_path(self):
        """Overlay's path->digest wins over base."""
        base = _make_stage({"old_digest": ["file.txt"]})
        overlay = _make_stage({"new_digest": ["file.txt"]})
        merged = base.overlay(overlay)
        assert "new_digest" in merged.state
        assert merged.state["new_digest"] == ["file.txt"]
        assert "old_digest" not in merged.state

    def test_overlay_multiple_stages(self):
        s1 = _make_stage({"d1": ["a.txt"]})
        s2 = _make_stage({"d2": ["b.txt"]})
        s3 = _make_stage({"d3": ["c.txt"]})
        merged = s1.overlay(s2, s3)
        paths = {p for ps in merged.state.values() for p in ps}
        assert paths == {"a.txt", "b.txt", "c.txt"}

    def test_overlay_algorithm_mismatch_raises(self):
        s1 = _make_stage({"d1": ["a.txt"]}, alg=DigestAlgorithm.SHA512)
        s2 = _make_stage({"d2": ["b.txt"]}, alg=DigestAlgorithm.SHA256)
        with pytest.raises(ValueError, match="digest_algorithm"):
            s1.overlay(s2)

    def test_overlay_content_source_chain(self):
        """Content source tries each stage's source in order."""
        fs1 = _DummyFS()
        fs2 = _DummyFS()
        s1 = _make_stage(
            {"d1": ["a.txt"]},
            files={"d1": (fs1, "path1")},
        )
        s2 = _make_stage(
            {"d2": ["b.txt"]},
            files={"d2": (fs2, "path2")},
        )
        merged = s1.overlay(s2)
        result_fs, _ = merged.content_source.get_content("d1")
        assert result_fs is fs1
        result_fs, _ = merged.content_source.get_content("d2")
        assert result_fs is fs2

    def test_overlay_fixity_chain(self):
        """Fixity source chains through all stages."""
        s1 = _make_stage(
            {"d1": ["a.txt"]},
            fixity={"d1": {DigestAlgorithm.SHA256: "aaa"}},
        )
        s2 = _make_stage(
            {"d2": ["b.txt"]},
            fixity={"d2": {DigestAlgorithm.SHA256: "bbb"}},
        )
        merged = s1.overlay(s2)
        assert merged.fixity_source is not None
        assert merged.fixity_source.get_fixity("d1") == {DigestAlgorithm.SHA256: "aaa"}
        assert merged.fixity_source.get_fixity("d2") == {DigestAlgorithm.SHA256: "bbb"}

    def test_overlay_no_fixity(self):
        """If no stages have fixity, result has none."""
        s1 = _make_stage({"d1": ["a.txt"]})
        s2 = _make_stage({"d2": ["b.txt"]})
        merged = s1.overlay(s2)
        assert merged.fixity_source is None

    def test_overlay_preserves_algorithm(self):
        s1 = _make_stage({"d1": ["a.txt"]}, alg=DigestAlgorithm.SHA256)
        s2 = _make_stage({"d2": ["b.txt"]}, alg=DigestAlgorithm.SHA256)
        merged = s1.overlay(s2)
        assert merged.digest_algorithm is DigestAlgorithm.SHA256


class TestObjectVersionState:
    def test_version_stage_state_matches(self, good_objects_1_1: Path):
        """version_stage state matches version_state."""
        obj_path = good_objects_1_1 / "spec-ex-full"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        v1 = VNum(1)
        stage = obj.version_state(v1)
        assert stage.state == obj.version_info(v1).state

    def test_version_stage_algorithm(self, good_objects_1_1: Path):
        """version_stage uses object's digest algorithm."""
        obj_path = good_objects_1_1 / "spec-ex-full"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        stage = obj.version_state(VNum(1))
        assert stage.digest_algorithm is DigestAlgorithm.SHA512

    def test_version_stage_content_source(self, good_objects_1_1: Path):
        """Content source from version_stage can open files."""
        obj_path = good_objects_1_1 / "spec-ex-full"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        v1 = VNum(1)
        stage = obj.version_state(v1)

        # Get first digest from state
        digest = next(iter(stage.state))
        src_fs, src_path = stage.content_source.get_content(digest)
        # Should be able to open and read the content
        with src_fs.open_file(src_path) as f:
            data = f.read()
        assert len(data) > 0

    def test_version_stage_head(self, good_objects_1_1: Path):
        """version_stage works for head version."""
        obj_path = good_objects_1_1 / "spec-ex-full"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        stage = obj.version_state(obj.head)
        assert stage.state == obj.version_info(obj.head).state

    def test_version_stage_invalid_version(self, good_objects_1_1: Path):
        """version_stage raises VersionNotFound for nonexistent version."""
        from ocfl_lib._errors import VersionNotFound

        obj_path = good_objects_1_1 / "spec-ex-full"
        fsys = LocalFS(obj_path)
        obj = Object(fsys, "")
        with pytest.raises(VersionNotFound):
            obj.version_state(VNum(99))
