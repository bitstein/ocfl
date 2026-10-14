from digests import D1, D2

from ocfl_lib.validation._inventory import validate_inventory_against_prior
from ocfl_lib.validation._report import FindingCollector


def _inv(*, versions, head=None, id="http://example.org/obj1", manifest=None):
    """Build inventory dict."""
    if head is None:
        head = max(versions.keys())
    if manifest is None:
        manifest = {D1: ["v1/content/file.txt"]}
    return {
        "id": id,
        "type": "https://ocfl.io/1.1/spec/#inventory",
        "digestAlgorithm": "sha512",
        "head": head,
        "manifest": manifest,
        "versions": versions,
    }


def _v1_block(**overrides):
    base = {"created": "2024-01-01T00:00:00Z", "state": {D1: ["file.txt"]}}
    base.update(overrides)
    return base


def _v2_block(**overrides):
    base = {
        "created": "2024-01-02T00:00:00Z",
        "state": {D1: ["file.txt"], D2: ["file2.txt"]},
    }
    base.update(overrides)
    return base


class TestIdConsistency:
    def test_id_changed(self):
        prior = _inv(id="http://example.org/obj1", versions={"v1": _v1_block()})
        current = _inv(
            id="http://example.org/CHANGED",
            manifest={D1: ["v1/content/file.txt"], D2: ["v2/content/file2.txt"]},
            versions={"v1": _v1_block(), "v2": _v2_block()},
        )
        r = FindingCollector()
        validate_inventory_against_prior(current, prior, r)
        assert r.has_code("E037b")

    def test_id_same_no_error(self):
        prior = _inv(versions={"v1": _v1_block()})
        current = _inv(
            manifest={D1: ["v1/content/file.txt"], D2: ["v2/content/file2.txt"]},
            versions={"v1": _v1_block(), "v2": _v2_block()},
        )
        r = FindingCollector()
        validate_inventory_against_prior(current, prior, r)
        assert not r.has_code("E037b")


class TestVersionSubset:
    def test_prior_version_missing_in_current(self):
        prior = _inv(
            manifest={D1: ["v1/content/file.txt"], D2: ["v2/content/file2.txt"]},
            versions={"v1": _v1_block(), "v2": _v2_block()},
        )
        current = _inv(versions={"v1": _v1_block()})
        r = FindingCollector()
        validate_inventory_against_prior(current, prior, r)
        assert r.has_code("E066a")


class TestVersionStateConsistency:
    def test_logical_paths_differ(self):
        prior = _inv(versions={"v1": _v1_block()})
        current = _inv(
            manifest={D1: ["v1/content/file.txt"], D2: ["v2/content/file2.txt"]},
            versions={
                "v1": _v1_block(state={D1: ["different.txt"]}),
                "v2": _v2_block(),
            },
        )
        r = FindingCollector()
        validate_inventory_against_prior(current, prior, r)
        assert r.has_code("E066b")

    def test_prior_logical_path_missing_from_current(self):
        prior = _inv(versions={"v1": _v1_block(state={D1: ["prior-only.txt"]})})
        current = _inv(
            manifest={D1: ["v1/content/file.txt"], D2: ["v2/content/file2.txt"]},
            versions={
                "v1": _v1_block(state={D1: []}),
                "v2": _v2_block(),
            },
        )
        r = FindingCollector()
        validate_inventory_against_prior(current, prior, r)
        e066b = [e for e in r.errors if e.code == "E066b"]
        assert len(e066b) == 1
        assert "prior-only.txt" in e066b[0].render()
        assert "only in v1 inventory" in e066b[0].render()

    def test_current_logical_path_added_to_prior_version(self):
        prior = _inv(versions={"v1": _v1_block(state={D1: []})})
        current = _inv(
            manifest={D1: ["v1/content/file.txt"], D2: ["v2/content/file2.txt"]},
            versions={
                "v1": _v1_block(state={D1: ["current-only.txt"]}),
                "v2": _v2_block(),
            },
        )
        r = FindingCollector()
        validate_inventory_against_prior(current, prior, r)
        e066b = [e for e in r.errors if e.code == "E066b"]
        assert len(e066b) == 1
        assert "current-only.txt" in e066b[0].render()
        assert "only in root inventory" in e066b[0].render()

    def test_matching_versions_no_error(self):
        prior = _inv(versions={"v1": _v1_block()})
        current = _inv(
            manifest={D1: ["v1/content/file.txt"], D2: ["v2/content/file2.txt"]},
            versions={"v1": _v1_block(), "v2": _v2_block()},
        )
        r = FindingCollector()
        validate_inventory_against_prior(current, prior, r)
        assert not r.has_code("E066")
