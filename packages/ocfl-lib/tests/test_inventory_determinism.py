"""Tests for byte-identical StoredInventory regardless of insertion order."""

import json

from ocfl_lib.digest import DigestAlgorithm
from ocfl_lib.inventory import Inventory, StoredInventory, VersionInfo


def _make_inventory(manifest, versions, *, fixity=None):
    return Inventory(
        id="http://example.org/obj1",
        type="https://ocfl.io/1.1/spec/#inventory",
        digest_algorithm="sha512",
        head="v1",
        manifest=manifest,
        versions=versions,
        fixity=fixity,
    )


ALG = DigestAlgorithm.SHA512


class TestPathListDeterminism:
    """Same (digest, paths) in different insertion order produce identical bytes."""

    def test_manifest_path_order(self):
        """Manifest path lists sorted regardless of insertion order."""
        inv_a = _make_inventory(
            manifest={"aaa": ["z.txt", "a.txt", "m.txt"]},
            versions={"v1": VersionInfo(
                created="2024-01-01T00:00:00Z",
                state={"aaa": ["file.txt"]},
            )},
        )
        inv_b = _make_inventory(
            manifest={"aaa": ["m.txt", "a.txt", "z.txt"]},
            versions={"v1": VersionInfo(
                created="2024-01-01T00:00:00Z",
                state={"aaa": ["file.txt"]},
            )},
        )
        sa = StoredInventory.from_inventory(inv_a, ALG)
        sb = StoredInventory.from_inventory(inv_b, ALG)
        assert sa.raw_bytes == sb.raw_bytes
        parsed = json.loads(sa.raw_bytes)
        assert parsed["manifest"]["aaa"] == ["a.txt", "m.txt", "z.txt"]

    def test_state_path_order(self):
        """Version state path lists sorted regardless of insertion order."""
        inv_a = _make_inventory(
            manifest={"aaa": ["v1/content/file.txt"]},
            versions={"v1": VersionInfo(
                created="2024-01-01T00:00:00Z",
                state={"aaa": ["z.txt", "a.txt"]},
            )},
        )
        inv_b = _make_inventory(
            manifest={"aaa": ["v1/content/file.txt"]},
            versions={"v1": VersionInfo(
                created="2024-01-01T00:00:00Z",
                state={"aaa": ["a.txt", "z.txt"]},
            )},
        )
        sa = StoredInventory.from_inventory(inv_a, ALG)
        sb = StoredInventory.from_inventory(inv_b, ALG)
        assert sa.raw_bytes == sb.raw_bytes

    def test_fixity_path_order(self):
        """Fixity path lists sorted regardless of insertion order."""
        inv_a = _make_inventory(
            manifest={"aaa": ["v1/content/file.txt"]},
            versions={"v1": VersionInfo(
                created="2024-01-01T00:00:00Z",
                state={"aaa": ["file.txt"]},
            )},
            fixity={"md5": {"fff": ["z.txt", "a.txt"]}},
        )
        inv_b = _make_inventory(
            manifest={"aaa": ["v1/content/file.txt"]},
            versions={"v1": VersionInfo(
                created="2024-01-01T00:00:00Z",
                state={"aaa": ["file.txt"]},
            )},
            fixity={"md5": {"fff": ["a.txt", "z.txt"]}},
        )
        sa = StoredInventory.from_inventory(inv_a, ALG)
        sb = StoredInventory.from_inventory(inv_b, ALG)
        assert sa.raw_bytes == sb.raw_bytes
        parsed = json.loads(sa.raw_bytes)
        assert parsed["fixity"]["md5"]["fff"] == ["a.txt", "z.txt"]

    def test_multiple_versions_path_order(self):
        """Path lists sorted across multiple versions."""
        inv_a = _make_inventory(
            manifest={
                "aaa": ["v1/content/file.txt"],
                "bbb": ["v2/content/new.txt"],
            },
            versions={
                "v1": VersionInfo(
                    created="2024-01-01T00:00:00Z",
                    state={"aaa": ["c.txt", "a.txt"]},
                ),
                "v2": VersionInfo(
                    created="2024-06-01T00:00:00Z",
                    state={"bbb": ["z.txt", "b.txt"]},
                ),
            },
        )
        inv_b = _make_inventory(
            manifest={
                "aaa": ["v1/content/file.txt"],
                "bbb": ["v2/content/new.txt"],
            },
            versions={
                "v1": VersionInfo(
                    created="2024-01-01T00:00:00Z",
                    state={"aaa": ["a.txt", "c.txt"]},
                ),
                "v2": VersionInfo(
                    created="2024-06-01T00:00:00Z",
                    state={"bbb": ["b.txt", "z.txt"]},
                ),
            },
        )
        sa = StoredInventory.from_inventory(inv_a, ALG)
        sb = StoredInventory.from_inventory(inv_b, ALG)
        assert sa.raw_bytes == sb.raw_bytes

    def test_multi_digest_all_sorted(self):
        """Multiple digests each with shuffled paths all get sorted."""
        inv = _make_inventory(
            manifest={
                "aaa": ["z.txt", "a.txt"],
                "bbb": ["y.txt", "b.txt"],
            },
            versions={"v1": VersionInfo(
                created="2024-01-01T00:00:00Z",
                state={
                    "aaa": ["z.txt", "a.txt"],
                    "bbb": ["y.txt", "b.txt"],
                },
            )},
        )
        parsed = json.loads(
            StoredInventory.from_inventory(inv, ALG).raw_bytes
        )
        assert parsed["manifest"]["aaa"] == ["a.txt", "z.txt"]
        assert parsed["manifest"]["bbb"] == ["b.txt", "y.txt"]
        assert parsed["versions"]["v1"]["state"]["aaa"] == ["a.txt", "z.txt"]
        assert parsed["versions"]["v1"]["state"]["bbb"] == ["b.txt", "y.txt"]
