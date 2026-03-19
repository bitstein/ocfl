"""Tests for inventory validation — versions checks."""

from ocfl_lib.validation.inventory import validate_inventory
from ocfl_lib.validation.result import ValidationResult

_D1 = "a" * 128
_D2 = "b" * 128


def _inv(**overrides):
    """Minimal valid inventory with overrides."""
    base = {
        "id": "http://example.org/obj1",
        "type": "https://ocfl.io/1.1/spec/#inventory",
        "digestAlgorithm": "sha512",
        "head": "v1",
        "manifest": {_D1: ["v1/content/file.txt"]},
        "versions": {
            "v1": {
                "created": "2024-01-01T00:00:00Z",
                "state": {_D1: ["file.txt"]},
            },
        },
    }
    base.update(overrides)
    return base


class TestVersionKeys:
    def test_invalid_version_key(self):
        r = ValidationResult()
        validate_inventory(
            _inv(versions={"bad": {"created": "2024-01-01T00:00:00Z", "state": {}}}),
            r,
        )
        assert r.has_code("E046")

    def test_must_start_at_v1(self):
        r = ValidationResult()
        validate_inventory(
            _inv(
                head="v2",
                versions={
                    "v2": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {_D1: ["file.txt"]},
                    },
                },
            ),
            r,
        )
        assert r.has_code("E009")

    def test_contiguous_versions(self):
        r = ValidationResult()
        validate_inventory(
            _inv(
                head="v3",
                manifest={
                    _D1: ["v1/content/file.txt"],
                    _D2: ["v3/content/file2.txt"],
                },
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {_D1: ["file.txt"]},
                    },
                    "v3": {
                        "created": "2024-01-02T00:00:00Z",
                        "state": {_D1: ["file.txt"], _D2: ["file2.txt"]},
                    },
                },
            ),
            r,
        )
        assert r.has_code("E010")

    def test_zero_padded_warning(self):
        r = ValidationResult()
        validate_inventory(
            _inv(
                head="v001",
                versions={
                    "v001": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {_D1: ["file.txt"]},
                    },
                },
            ),
            r,
        )
        assert r.has_code("W001")


class TestHead:
    def test_head_not_highest(self):
        r = ValidationResult()
        validate_inventory(
            _inv(
                head="v1",
                manifest={
                    _D1: ["v1/content/file.txt"],
                    _D2: ["v2/content/file2.txt"],
                },
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {_D1: ["file.txt"]},
                    },
                    "v2": {
                        "created": "2024-01-02T00:00:00Z",
                        "state": {_D1: ["file.txt"], _D2: ["file2.txt"]},
                    },
                },
            ),
            r,
        )
        assert r.has_code("E040")


class TestVersionBlock:
    def test_missing_created(self):
        r = ValidationResult()
        validate_inventory(_inv(versions={"v1": {"state": {_D1: ["file.txt"]}}}), r)
        assert r.has_code("E048")

    def test_non_string_created(self):
        r = ValidationResult()
        validate_inventory(
            _inv(
                versions={
                    "v1": {
                        "created": 12345,
                        "state": {_D1: ["file.txt"]},
                    },
                }
            ),
            r,
        )
        assert r.has_code("E049d")

    def test_created_no_timezone(self):
        r = ValidationResult()
        validate_inventory(
            _inv(
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00",
                        "state": {_D1: ["file.txt"]},
                    },
                }
            ),
            r,
        )
        assert r.has_code("E049a")

    def test_missing_state(self):
        r = ValidationResult()
        validate_inventory(
            _inv(versions={"v1": {"created": "2024-01-01T00:00:00Z"}}), r
        )
        assert r.has_code("E048c")

    def test_state_not_object(self):
        r = ValidationResult()
        validate_inventory(
            _inv(
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": "bad",
                    },
                }
            ),
            r,
        )
        assert r.has_code("E050c")


class TestStateDigests:
    def test_state_digest_not_in_manifest(self):
        r = ValidationResult()
        validate_inventory(
            _inv(
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {"z" * 128: ["file.txt"]},
                    },
                }
            ),
            r,
        )
        assert r.has_code("E050f") or r.has_code("E050a")

    def test_state_value_not_list(self):
        r = ValidationResult()
        validate_inventory(
            _inv(
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {_D1: "file.txt"},
                    },
                }
            ),
            r,
        )
        assert r.has_code("E050e")


class TestLogicalPaths:
    def test_dot_in_path(self):
        r = ValidationResult()
        validate_inventory(
            _inv(
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {_D1: ["./file.txt"]},
                    },
                }
            ),
            r,
        )
        assert r.has_code("E052") or r.has_code("E053")

    def test_dotdot_in_path(self):
        r = ValidationResult()
        validate_inventory(
            _inv(
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {_D1: ["../file.txt"]},
                    },
                }
            ),
            r,
        )
        assert r.has_code("E052") or r.has_code("E053")

    def test_duplicate_logical_path(self):
        r = ValidationResult()
        validate_inventory(
            _inv(
                manifest={_D1: ["v1/content/a.txt"], _D2: ["v1/content/b.txt"]},
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {_D1: ["file.txt"], _D2: ["file.txt"]},
                    },
                },
            ),
            r,
        )
        assert r.has_code("E095")


class TestVersionMetadata:
    def test_no_message_warning(self):
        r = ValidationResult()
        validate_inventory(
            _inv(
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {_D1: ["file.txt"]},
                    },
                }
            ),
            r,
        )
        assert r.has_code("W007a")

    def test_no_user_warning(self):
        r = ValidationResult()
        validate_inventory(
            _inv(
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {_D1: ["file.txt"]},
                    },
                }
            ),
            r,
        )
        assert r.has_code("W007b")

    def test_user_not_object(self):
        r = ValidationResult()
        validate_inventory(
            _inv(
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {_D1: ["file.txt"]},
                        "user": "not an object",
                    },
                }
            ),
            r,
        )
        assert r.has_code("E054a")

    def test_user_address_not_uri(self):
        r = ValidationResult()
        validate_inventory(
            _inv(
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {_D1: ["file.txt"]},
                        "message": "test",
                        "user": {"name": "Test", "address": "not-a-uri"},
                    },
                }
            ),
            r,
        )
        assert r.has_code("W009")


class TestManifestUsage:
    def test_unreferenced_manifest_digest(self):
        r = ValidationResult()
        validate_inventory(
            _inv(
                manifest={
                    _D1: ["v1/content/file.txt"],
                    _D2: ["v1/content/unused.txt"],
                },
            ),
            r,
        )
        assert r.has_code("E107")
