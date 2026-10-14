from digests import D1, D2

from ocfl_lib.validation._inventory import validate_inventory
from ocfl_lib.validation._report import FindingCollector


def _inv(**overrides):
    """Minimal valid inventory with overrides."""
    base = {
        "id": "http://example.org/obj1",
        "type": "https://ocfl.io/1.1/spec/#inventory",
        "digestAlgorithm": "sha512",
        "head": "v1",
        "manifest": {D1: ["v1/content/file.txt"]},
        "versions": {
            "v1": {
                "created": "2024-01-01T00:00:00Z",
                "state": {D1: ["file.txt"]},
            },
        },
    }
    base.update(overrides)
    return base


class TestVersionKeys:
    def test_invalid_version_key(self):
        r = FindingCollector()
        validate_inventory(
            _inv(versions={"bad": {"created": "2024-01-01T00:00:00Z", "state": {}}}),
            r,
        )
        assert r.has_code("E046")

    def test_must_start_at_v1(self):
        r = FindingCollector()
        validate_inventory(
            _inv(
                head="v2",
                versions={
                    "v2": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {D1: ["file.txt"]},
                    },
                },
            ),
            r,
        )
        assert r.has_code("E009")

    def test_contiguous_versions(self):
        r = FindingCollector()
        validate_inventory(
            _inv(
                head="v3",
                manifest={
                    D1: ["v1/content/file.txt"],
                    D2: ["v3/content/file2.txt"],
                },
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {D1: ["file.txt"]},
                    },
                    "v3": {
                        "created": "2024-01-02T00:00:00Z",
                        "state": {D1: ["file.txt"], D2: ["file2.txt"]},
                    },
                },
            ),
            r,
        )
        assert r.has_code("E010")

    def test_empty_versions_still_reported_when_head_missing(self):
        inv = _inv(versions={})
        del inv["head"]
        r = FindingCollector()
        validate_inventory(inv, r)
        assert r.has_code("E008")
        assert r.has_code("E036")

    def test_zero_padded_warning(self):
        r = FindingCollector()
        validate_inventory(
            _inv(
                head="v001",
                versions={
                    "v001": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {D1: ["file.txt"]},
                    },
                },
            ),
            r,
        )
        assert r.has_code("W001")

    def test_mixed_padding_emits_E011_E012_E013(self):
        r = FindingCollector()
        validate_inventory(
            _inv(
                head="v2",
                manifest={
                    D1: ["v01/content/file.txt"],
                    D2: ["v2/content/file2.txt"],
                },
                versions={
                    "v01": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {D1: ["file.txt"]},
                    },
                    "v2": {
                        "created": "2024-01-02T00:00:00Z",
                        "state": {D1: ["file.txt"], D2: ["file2.txt"]},
                    },
                },
            ),
            r,
        )
        assert r.has_code("E011")
        assert r.has_code("E012")
        assert r.has_code("E013")

    def test_inconsistent_pad_width_emits_E012(self):
        r = FindingCollector()
        validate_inventory(
            _inv(
                head="v002",
                manifest={
                    D1: ["v01/content/file.txt"],
                    D2: ["v002/content/file2.txt"],
                },
                versions={
                    "v01": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {D1: ["file.txt"]},
                    },
                    "v002": {
                        "created": "2024-01-02T00:00:00Z",
                        "state": {D1: ["file.txt"], D2: ["file2.txt"]},
                    },
                },
            ),
            r,
        )
        assert r.has_code("E012")
        assert not r.has_code("E011")

    def test_consistent_padding_no_E012(self):
        r = FindingCollector()
        validate_inventory(
            _inv(
                head="v002",
                manifest={
                    D1: ["v001/content/file.txt"],
                    D2: ["v002/content/file2.txt"],
                },
                versions={
                    "v001": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {D1: ["file.txt"]},
                    },
                    "v002": {
                        "created": "2024-01-02T00:00:00Z",
                        "state": {D1: ["file.txt"], D2: ["file2.txt"]},
                    },
                },
            ),
            r,
        )
        assert not r.has_code("E012")
        assert r.has_code("W001")


class TestHead:
    def test_head_not_highest(self):
        r = FindingCollector()
        validate_inventory(
            _inv(
                head="v1",
                manifest={
                    D1: ["v1/content/file.txt"],
                    D2: ["v2/content/file2.txt"],
                },
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {D1: ["file.txt"]},
                    },
                    "v2": {
                        "created": "2024-01-02T00:00:00Z",
                        "state": {D1: ["file.txt"], D2: ["file2.txt"]},
                    },
                },
            ),
            r,
        )
        assert r.has_code("E040")


class TestVersionBlock:
    def test_missing_created(self):
        r = FindingCollector()
        validate_inventory(_inv(versions={"v1": {"state": {D1: ["file.txt"]}}}), r)
        assert r.has_code("E048")

    def test_non_string_created(self):
        r = FindingCollector()
        validate_inventory(
            _inv(
                versions={
                    "v1": {
                        "created": 12345,
                        "state": {D1: ["file.txt"]},
                    },
                }
            ),
            r,
        )
        assert r.has_code("E049d")

    def test_created_no_timezone(self):
        r = FindingCollector()
        validate_inventory(
            _inv(
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00",
                        "state": {D1: ["file.txt"]},
                    },
                }
            ),
            r,
        )
        assert r.has_code("E049a")

    def test_created_invalid_iso_rejected(self):
        r = FindingCollector()
        validate_inventory(
            _inv(
                versions={
                    "v1": {
                        "created": "not-a-date",
                        "state": {D1: ["file.txt"]},
                    },
                }
            ),
            r,
        )
        assert r.has_code("E049b")

    def test_created_with_offset_tz_accepted(self):
        r = FindingCollector()
        validate_inventory(
            _inv(
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00+02:00",
                        "state": {D1: ["file.txt"]},
                    },
                }
            ),
            r,
        )
        assert not r.has_code("E049a")
        assert not r.has_code("E049b")

    def test_created_with_offset_requires_seconds(self):
        r = FindingCollector()
        validate_inventory(
            _inv(
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00+00:00",
                        "state": {D1: ["file.txt"]},
                    },
                }
            ),
            r,
        )
        assert r.has_code("E049b")

    def test_missing_state(self):
        r = FindingCollector()
        validate_inventory(
            _inv(versions={"v1": {"created": "2024-01-01T00:00:00Z"}}), r
        )
        assert r.has_code("E048c")

    def test_state_not_object(self):
        r = FindingCollector()
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
        r = FindingCollector()
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
        r = FindingCollector()
        validate_inventory(
            _inv(
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {D1: "file.txt"},
                    },
                }
            ),
            r,
        )
        assert r.has_code("E050e")


class TestLogicalPaths:
    def test_dot_in_path(self):
        r = FindingCollector()
        validate_inventory(
            _inv(
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {D1: ["./file.txt"]},
                    },
                }
            ),
            r,
        )
        assert r.has_code("E052")
        assert not r.has_code("E053")

    def test_dotdot_in_path(self):
        r = FindingCollector()
        validate_inventory(
            _inv(
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {D1: ["../file.txt"]},
                    },
                }
            ),
            r,
        )
        assert r.has_code("E052")
        assert not r.has_code("E053")

    def test_leading_slash_path(self):
        r = FindingCollector()
        validate_inventory(
            _inv(
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {D1: ["/file.txt"]},
                    },
                }
            ),
            r,
        )
        assert r.has_code("E053")
        assert not r.has_code("E052")

    def test_duplicate_logical_path(self):
        r = FindingCollector()
        validate_inventory(
            _inv(
                manifest={D1: ["v1/content/a.txt"], D2: ["v1/content/b.txt"]},
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {D1: ["file.txt"], D2: ["file.txt"]},
                    },
                },
            ),
            r,
        )
        assert r.has_code("E095")

    def test_non_adjacent_prefix_conflict(self):
        r = FindingCollector()
        validate_inventory(
            _inv(
                manifest={D1: ["v1/content/a", "v1/content/a!", "v1/content/a/b"]},
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {D1: ["a", "a!", "a/b"]},
                    },
                },
            ),
            r,
        )
        assert r.has_code("E095b")


class TestVersionMetadata:
    def test_no_message_warning(self):
        r = FindingCollector()
        validate_inventory(
            _inv(
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {D1: ["file.txt"]},
                    },
                }
            ),
            r,
        )
        assert r.has_code("W007a")

    def test_no_user_warning(self):
        r = FindingCollector()
        validate_inventory(
            _inv(
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {D1: ["file.txt"]},
                    },
                }
            ),
            r,
        )
        assert r.has_code("W007b")

    def test_user_not_object(self):
        r = FindingCollector()
        validate_inventory(
            _inv(
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {D1: ["file.txt"]},
                        "user": "not an object",
                    },
                }
            ),
            r,
        )
        assert r.has_code("E054a")

    def test_user_address_not_uri(self):
        r = FindingCollector()
        validate_inventory(
            _inv(
                versions={
                    "v1": {
                        "created": "2024-01-01T00:00:00Z",
                        "state": {D1: ["file.txt"]},
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
        r = FindingCollector()
        validate_inventory(
            _inv(
                manifest={
                    D1: ["v1/content/file.txt"],
                    D2: ["v1/content/unused.txt"],
                },
            ),
            r,
        )
        assert r.has_code("E107")
