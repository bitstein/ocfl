"""Tests for DigestMap operations."""

from ocfl_lib._digest_map import (
    DigestMap,
    PathMap,
    eq,
    from_path_map,
    merge,
    normalize,
    paths,
    remove_path,
    rename_paths,
    to_path_map,
    validate,
)


class TestNormalize:
    def test_lowercases_digests(self):
        dm: DigestMap = {"ABC123": ["file.txt"]}
        result = normalize(dm)
        assert "abc123" in result
        assert "ABC123" not in result

    def test_sorts_paths(self):
        dm: DigestMap = {"abc": ["z.txt", "a.txt", "m.txt"]}
        result = normalize(dm)
        assert result["abc"] == ["a.txt", "m.txt", "z.txt"]

    def test_empty(self):
        assert normalize({}) == {}

    def test_preserves_multiple_digests(self):
        dm: DigestMap = {"BBB": ["b.txt"], "AAA": ["a.txt"]}
        result = normalize(dm)
        assert set(result.keys()) == {"aaa", "bbb"}

    def test_merges_case_colliding_keys(self):
        dm: DigestMap = {"ABC": ["a.txt"], "abc": ["b.txt"]}
        result = normalize(dm)
        assert "abc" in result
        assert sorted(result["abc"]) == ["a.txt", "b.txt"]


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
        assert eq(result, dm)

    def test_empty(self):
        assert from_path_map({}) == {}


class TestMerge:
    def test_disjoint(self):
        a: DigestMap = {"abc": ["a.txt"]}
        b: DigestMap = {"def": ["b.txt"]}
        result = merge(a, b)
        assert result == {"abc": ["a.txt"], "def": ["b.txt"]}

    def test_same_digest_combines_paths(self):
        a: DigestMap = {"abc": ["a.txt"]}
        b: DigestMap = {"abc": ["b.txt"]}
        result = merge(a, b)
        assert sorted(result["abc"]) == ["a.txt", "b.txt"]

    def test_replace_mode(self):
        a: DigestMap = {"abc": ["a.txt"]}
        b: DigestMap = {"def": ["a.txt"]}
        result = merge(a, b, replace=True)
        # b's mapping for a.txt should win
        pm = to_path_map(result)
        assert pm["a.txt"] == "def"

    def test_empty_base(self):
        b: DigestMap = {"abc": ["a.txt"]}
        assert merge({}, b) == b

    def test_empty_overlay(self):
        a: DigestMap = {"abc": ["a.txt"]}
        assert merge(a, {}) == a

    def test_no_duplicate_paths(self):
        a: DigestMap = {"abc": ["a.txt"]}
        b: DigestMap = {"abc": ["a.txt"]}
        result = merge(a, b)
        assert result["abc"] == ["a.txt"]


class TestPaths:
    def test_yields_all_path_digest_pairs(self):
        dm: DigestMap = {"abc": ["a.txt", "b.txt"], "def": ["c.txt"]}
        result = sorted(paths(dm))
        assert result == [("a.txt", "abc"), ("b.txt", "abc"), ("c.txt", "def")]

    def test_empty(self):
        assert list(paths({})) == []


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
        """'old' prefix should not match 'older'."""
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
        # rename y -> x causes both to land at x/a.txt
        with pytest.raises(ValueError, match="collision"):
            rename_paths(dm, "y", "x")


class TestRemovePath:
    def test_removes_path(self):
        dm: DigestMap = {"abc": ["a.txt", "b.txt"]}
        result = remove_path(dm, "a.txt")
        assert result == {"abc": ["b.txt"]}

    def test_removes_digest_when_last_path(self):
        dm: DigestMap = {"abc": ["a.txt"]}
        result = remove_path(dm, "a.txt")
        assert result == {}

    def test_path_not_found(self):
        dm: DigestMap = {"abc": ["a.txt"]}
        result = remove_path(dm, "nope.txt")
        assert result == dm

    def test_empty(self):
        assert remove_path({}, "a.txt") == {}


class TestEq:
    def test_identical(self):
        dm: DigestMap = {"abc": ["a.txt"]}
        assert eq(dm, dm)

    def test_case_insensitive_digests(self):
        a: DigestMap = {"ABC": ["a.txt"]}
        b: DigestMap = {"abc": ["a.txt"]}
        assert eq(a, b)

    def test_path_order_ignored(self):
        a: DigestMap = {"abc": ["b.txt", "a.txt"]}
        b: DigestMap = {"abc": ["a.txt", "b.txt"]}
        assert eq(a, b)

    def test_different_content(self):
        a: DigestMap = {"abc": ["a.txt"]}
        b: DigestMap = {"def": ["a.txt"]}
        assert not eq(a, b)

    def test_both_empty(self):
        assert eq({}, {})


class TestValidate:
    def test_valid(self):
        dm: DigestMap = {"abc": ["a.txt", "b.txt"], "def": ["c.txt"]}
        assert validate(dm) == []

    def test_empty_digest(self):
        dm: DigestMap = {"": ["a.txt"]}
        errors = validate(dm)
        assert len(errors) > 0

    def test_empty_path_list(self):
        dm: DigestMap = {"abc": []}
        errors = validate(dm)
        assert len(errors) > 0

    def test_empty_path_string(self):
        dm: DigestMap = {"abc": [""]}
        errors = validate(dm)
        assert len(errors) > 0

    def test_duplicate_paths_across_digests(self):
        dm: DigestMap = {"abc": ["a.txt"], "def": ["a.txt"]}
        errors = validate(dm)
        assert len(errors) > 0

    def test_empty_map_valid(self):
        assert validate({}) == []
