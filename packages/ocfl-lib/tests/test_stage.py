"""Tests for Stage data structure and FixitySource protocol."""

from ocfl_lib.digest import DigestAlgorithm
from ocfl_lib.stage import FixitySource, Stage
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
