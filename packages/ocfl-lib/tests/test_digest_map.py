from ocfl_lib._digest_map import (
    DigestMap,
    PathMap,
    from_path_map,
    overlay_paths,
    rename_paths,
    to_path_map,
)


class TestToPathMap:
    def test_basic(self):
        dm: DigestMap = {"abc": ["file1.txt", "file2.txt"]}
        result = to_path_map(dm)
        assert result == {"file1.txt": "abc", "file2.txt": "abc"}

    def test_multiple_digests(self):
        dm: DigestMap = {"abc": ["a.txt"], "def": ["b.txt"]}
        result = to_path_map(dm)
        assert result == {"a.txt": "abc", "b.txt": "def"}

    def test_empty(self):
        assert to_path_map({}) == {}


class TestFromPathMap:
    def test_basic(self):
        pm: PathMap = {"file1.txt": "abc", "file2.txt": "abc"}
        result = from_path_map(pm)
        assert "abc" in result
        assert sorted(result["abc"]) == ["file1.txt", "file2.txt"]

    def test_multiple_digests(self):
        pm: PathMap = {"a.txt": "abc", "b.txt": "def"}
        result = from_path_map(pm)
        assert result["abc"] == ["a.txt"]
        assert result["def"] == ["b.txt"]

    def test_round_trip(self):
        dm: DigestMap = {"abc": ["a.txt", "b.txt"], "def": ["c.txt"]}
        result = from_path_map(to_path_map(dm))
        assert to_path_map(result) == to_path_map(dm)

    def test_empty(self):
        assert from_path_map({}) == {}


class TestOverlayPaths:
    def test_disjoint(self):
        a: DigestMap = {"abc": ["a.txt"]}
        b: DigestMap = {"def": ["b.txt"]}
        result = overlay_paths(a, b)
        assert result == {"abc": ["a.txt"], "def": ["b.txt"]}

    def test_same_digest_combines_paths(self):
        a: DigestMap = {"abc": ["a.txt"]}
        b: DigestMap = {"abc": ["b.txt"]}
        result = overlay_paths(a, b)
        assert sorted(result["abc"]) == ["a.txt", "b.txt"]

    def test_overlay_path_overrides_base(self):
        a: DigestMap = {"abc": ["a.txt"]}
        b: DigestMap = {"def": ["a.txt"]}
        result = overlay_paths(a, b)
        pm = to_path_map(result)
        assert pm["a.txt"] == "def"

    def test_empty_base(self):
        b: DigestMap = {"abc": ["a.txt"]}
        assert overlay_paths({}, b) == b

    def test_empty_overlay(self):
        a: DigestMap = {"abc": ["a.txt"]}
        assert overlay_paths(a, {}) == a

    def test_no_duplicate_paths(self):
        a: DigestMap = {"abc": ["a.txt"]}
        b: DigestMap = {"abc": ["a.txt"]}
        result = overlay_paths(a, b)
        assert result["abc"] == ["a.txt"]


class TestRenamePaths:
    def test_rename_prefix(self):
        dm: DigestMap = {"abc": ["old/a.txt", "old/b.txt"], "def": ["other.txt"]}
        result = rename_paths(dm, "old", "new")
        pm = to_path_map(result)
        assert "new/a.txt" in pm
        assert "new/b.txt" in pm
        assert "other.txt" in pm

    def test_no_match(self):
        dm: DigestMap = {"abc": ["a.txt"]}
        result = rename_paths(dm, "old", "new")
        assert result == dm

    def test_does_not_rename_similar_prefix(self):
        dm: DigestMap = {"abc": ["older/a.txt"]}
        result = rename_paths(dm, "old", "new")
        assert result == dm

    def test_exact_path_match(self):
        dm: DigestMap = {"abc": ["old"]}
        result = rename_paths(dm, "old", "new")
        assert result == {"abc": ["new"]}

    def test_empty(self):
        assert rename_paths({}, "a", "b") == {}

    def test_collision_within_call_raises(self):
        import pytest

        dm: DigestMap = {"abc": ["x/a.txt"], "def": ["y/a.txt"]}
        with pytest.raises(ValueError, match="collision"):
            rename_paths(dm, "y", "x")
