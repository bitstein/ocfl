"""Tests for inventory cross-version validation."""

from ocfl_lib.validation.inventory import validate_inventory_against_prior
from ocfl_lib.validation.result import ValidationResult

_D1 = "a" * 128
_D2 = "b" * 128


def _inv(*, versions, head=None, id="http://example.org/obj1", manifest=None):
    """Build inventory dict."""
    if head is None:
        head = max(versions.keys())
    if manifest is None:
        manifest = {_D1: ["v1/content/file.txt"]}
    return {
        "id": id,
        "type": "https://ocfl.io/1.1/spec/#inventory",
        "digestAlgorithm": "sha512",
        "head": head,
        "manifest": manifest,
        "versions": versions,
    }


def _v1_block(**overrides):
    base = {"created": "2024-01-01T00:00:00Z", "state": {_D1: ["file.txt"]}}
    base.update(overrides)
    return base


def _v2_block(**overrides):
    base = {
        "created": "2024-01-02T00:00:00Z",
        "state": {_D1: ["file.txt"], _D2: ["file2.txt"]},
    }
    base.update(overrides)
    return base


class TestIdConsistency:
    def test_id_changed(self):
        prior = _inv(id="http://example.org/obj1", versions={"v1": _v1_block()})
        current = _inv(
            id="http://example.org/CHANGED",
            manifest={_D1: ["v1/content/file.txt"], _D2: ["v2/content/file2.txt"]},
            versions={"v1": _v1_block(), "v2": _v2_block()},
        )
        r = ValidationResult()
        validate_inventory_against_prior(current, prior, r)
        assert r.has_code("E110")

    def test_id_same_no_error(self):
        prior = _inv(versions={"v1": _v1_block()})
        current = _inv(
            manifest={_D1: ["v1/content/file.txt"], _D2: ["v2/content/file2.txt"]},
            versions={"v1": _v1_block(), "v2": _v2_block()},
        )
        r = ValidationResult()
        validate_inventory_against_prior(current, prior, r)
        assert not r.has_code("E110")


class TestVersionSubset:
    def test_prior_version_missing_in_current(self):
        prior = _inv(
            manifest={_D1: ["v1/content/file.txt"], _D2: ["v2/content/file2.txt"]},
            versions={"v1": _v1_block(), "v2": _v2_block()},
        )
        # Current only has v1 — missing v2
        current = _inv(versions={"v1": _v1_block()})
        r = ValidationResult()
        validate_inventory_against_prior(current, prior, r)
        assert r.has_code("E066a")


class TestVersionStateConsistency:
    def test_logical_paths_differ(self):
        prior = _inv(versions={"v1": _v1_block()})
        # Current v1 has different state
        current = _inv(
            manifest={_D1: ["v1/content/file.txt"], _D2: ["v2/content/file2.txt"]},
            versions={
                "v1": _v1_block(state={_D1: ["different.txt"]}),
                "v2": _v2_block(),
            },
        )
        r = ValidationResult()
        validate_inventory_against_prior(current, prior, r)
        assert r.has_code("E066b")

    def test_matching_versions_no_error(self):
        prior = _inv(versions={"v1": _v1_block()})
        current = _inv(
            manifest={_D1: ["v1/content/file.txt"], _D2: ["v2/content/file2.txt"]},
            versions={"v1": _v1_block(), "v2": _v2_block()},
        )
        r = ValidationResult()
        validate_inventory_against_prior(current, prior, r)
        assert not r.has_code("E066")
