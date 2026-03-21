"""Tests for inventory model."""

import json
import pathlib

import msgspec
import pytest

from ocfl_lib.digest import DigestAlgorithm, bytes_digest
from ocfl_lib.inventory import (
    Inventory,
    StoredInventory,
    UserInfo,
    VersionInfo,
)

# --- Minimal valid inventory data ---

MINIMAL_INV_DICT = {
    "id": "http://example.org/obj1",
    "type": "https://ocfl.io/1.1/spec/#inventory",
    "digestAlgorithm": "sha512",
    "head": "v1",
    "manifest": {
        "abc123": ["v1/content/file.txt"],
    },
    "versions": {
        "v1": {
            "created": "2024-01-01T00:00:00Z",
            "state": {
                "abc123": ["file.txt"],
            },
        },
    },
}


def _inv_json(**overrides: object) -> bytes:
    """Build inventory JSON bytes with optional field overrides."""
    d = {**MINIMAL_INV_DICT, **overrides}
    return json.dumps(d).encode()


# --- UserInfo ---


class TestUserInfo:
    def test_decode_full(self):
        data = b'{"name": "Alice", "address": "mailto:alice@example.org"}'
        u = msgspec.json.decode(data, type=UserInfo)
        assert u.name == "Alice"
        assert u.address == "mailto:alice@example.org"

    def test_decode_name_only(self):
        data = b'{"name": "Bob"}'
        u = msgspec.json.decode(data, type=UserInfo)
        assert u.name == "Bob"
        assert u.address is None

    def test_encode_round_trip(self):
        u = UserInfo(name="Alice", address="mailto:alice@example.org")
        data = msgspec.json.encode(u)
        u2 = msgspec.json.decode(data, type=UserInfo)
        assert u == u2

    def test_encode_omits_none_address(self):
        u = UserInfo(name="Bob")
        data = msgspec.json.decode(msgspec.json.encode(u))
        assert "address" not in data


# --- VersionInfo ---


class TestVersionInfo:
    def test_decode_full(self):
        data = b"""{
            "created": "2024-01-01T00:00:00Z",
            "state": {"abc123": ["file.txt"]},
            "message": "initial",
            "user": {"name": "Alice"}
        }"""
        v = msgspec.json.decode(data, type=VersionInfo)
        assert v.created == "2024-01-01T00:00:00Z"
        assert v.state == {"abc123": ["file.txt"]}
        assert v.message == "initial"
        assert v.user is not None
        assert v.user.name == "Alice"

    def test_decode_minimal(self):
        data = b'{"created": "2024-01-01T00:00:00Z", "state": {}}'
        v = msgspec.json.decode(data, type=VersionInfo)
        assert v.message is None
        assert v.user is None

    def test_encode_round_trip(self):
        v = VersionInfo(
            created="2024-01-01T00:00:00Z",
            state={"abc": ["f.txt"]},
            message="test",
            user=UserInfo(name="Bob"),
        )
        data = msgspec.json.encode(v)
        v2 = msgspec.json.decode(data, type=VersionInfo)
        assert v == v2

    def test_encode_omits_none_fields(self):
        v = VersionInfo(created="2024-01-01T00:00:00Z", state={})
        data = msgspec.json.decode(msgspec.json.encode(v))
        assert "message" not in data
        assert "user" not in data


# --- Inventory ---


class TestInventory:
    def test_decode_minimal(self):
        inv = msgspec.json.decode(_inv_json(), type=Inventory)
        assert inv.id == "http://example.org/obj1"
        assert inv.type == "https://ocfl.io/1.1/spec/#inventory"
        assert inv.digest_algorithm == "sha512"
        assert inv.head == "v1"
        assert inv.manifest == {"abc123": ["v1/content/file.txt"]}
        assert "v1" in inv.versions
        assert inv.content_directory is None
        assert inv.fixity is None

    def test_decode_camel_case_aliases(self):
        """digestAlgorithm and contentDirectory map to snake_case attrs."""
        data = _inv_json(contentDirectory="stuff")
        inv = msgspec.json.decode(data, type=Inventory)
        assert inv.digest_algorithm == "sha512"
        assert inv.content_directory == "stuff"

    def test_decode_with_fixity(self):
        fixity = {"md5": {"d41d8c": ["v1/content/file.txt"]}}
        inv = msgspec.json.decode(_inv_json(fixity=fixity), type=Inventory)
        assert inv.fixity == fixity

    def test_decode_full_fixture(self):
        """Decode a real OCFL fixture inventory."""
        fixture = (
            pathlib.Path(__file__).parents[3]
            / "tests"
            / "fixtures"
            / "1.1"
            / "good-objects"
            / "diff_files_same_md5"
            / "inventory.json"
        )
        if not fixture.exists():
            pytest.skip("fixtures not available")
        data = fixture.read_bytes()
        inv = msgspec.json.decode(data, type=Inventory)
        assert inv.id == "https://example.org/same_md5sum_example"
        assert inv.digest_algorithm == "sha512"
        assert inv.head == "v1"
        assert inv.fixity is not None
        assert "md5" in inv.fixity

    def test_encode_round_trip(self):
        inv = msgspec.json.decode(_inv_json(), type=Inventory)
        data = msgspec.json.encode(inv)
        inv2 = msgspec.json.decode(data, type=Inventory)
        assert inv == inv2

    def test_encode_uses_camel_case(self):
        inv = msgspec.json.decode(_inv_json(contentDirectory="stuff"), type=Inventory)
        encoded = msgspec.json.decode(msgspec.json.encode(inv))
        assert "digestAlgorithm" in encoded
        assert "contentDirectory" in encoded
        # snake_case should NOT appear
        assert "digest_algorithm" not in encoded
        assert "content_directory" not in encoded

    def test_encode_omits_none_optional_fields(self):
        inv = msgspec.json.decode(_inv_json(), type=Inventory)
        encoded = msgspec.json.decode(msgspec.json.encode(inv))
        assert "contentDirectory" not in encoded
        assert "fixity" not in encoded

    def test_decode_v1_0_type(self):
        inv = msgspec.json.decode(
            _inv_json(type="https://ocfl.io/1.0/spec/#inventory"),
            type=Inventory,
        )
        assert inv.type == "https://ocfl.io/1.0/spec/#inventory"

    def test_decode_multiple_versions(self):
        versions = {
            "v1": {
                "created": "2024-01-01T00:00:00Z",
                "state": {"abc": ["a.txt"]},
            },
            "v2": {
                "created": "2024-06-01T00:00:00Z",
                "state": {"abc": ["a.txt"], "def": ["b.txt"]},
                "message": "added b",
            },
        }
        inv = msgspec.json.decode(
            _inv_json(head="v2", versions=versions), type=Inventory
        )
        assert len(inv.versions) == 2
        assert inv.versions["v2"].message == "added b"

    def test_decode_rejects_missing_required(self):
        """Missing required fields should raise."""
        for field in ("id", "type", "digestAlgorithm", "head", "manifest", "versions"):
            d = {**MINIMAL_INV_DICT}
            del d[field]
            with pytest.raises(msgspec.ValidationError):
                msgspec.json.decode(json.dumps(d).encode(), type=Inventory)

    def test_decode_rejects_wrong_types(self):
        with pytest.raises(msgspec.ValidationError):
            msgspec.json.decode(_inv_json(id=123), type=Inventory)
        with pytest.raises(msgspec.ValidationError):
            msgspec.json.decode(_inv_json(manifest="not-a-dict"), type=Inventory)


# --- StoredInventory ---


class TestStoredInventory:
    def test_from_bytes_round_trip(self):
        """Raw bytes are preserved exactly."""
        raw = _inv_json()
        si = StoredInventory.from_bytes(raw, DigestAlgorithm.SHA512)
        assert si.raw_bytes == raw
        assert si.inventory.id == "http://example.org/obj1"
        assert si.digest_algorithm == DigestAlgorithm.SHA512

    def test_from_bytes_digest_matches(self):
        """Stored digest matches independent computation."""
        raw = _inv_json()
        si = StoredInventory.from_bytes(raw, DigestAlgorithm.SHA512)
        expected = bytes_digest(raw, DigestAlgorithm.SHA512)
        assert si.digest == expected

    def test_from_inventory_produces_compact_sorted_json(self):
        """from_inventory creates compact JSON with sorted keys."""
        inv = msgspec.json.decode(_inv_json(), type=Inventory)
        si = StoredInventory.from_inventory(inv, DigestAlgorithm.SHA512)
        # Verify it's valid JSON
        parsed = json.loads(si.raw_bytes)
        assert parsed["id"] == "http://example.org/obj1"
        # Verify sorted keys at top level
        keys = list(parsed.keys())
        assert keys == sorted(keys)
        # Verify compact (no whitespace)
        text = si.raw_bytes.decode()
        assert "\n" not in text
        assert "  " not in text

    def test_from_inventory_digest_correct(self):
        inv = msgspec.json.decode(_inv_json(), type=Inventory)
        si = StoredInventory.from_inventory(inv, DigestAlgorithm.SHA256)
        expected = bytes_digest(si.raw_bytes, DigestAlgorithm.SHA256)
        assert si.digest == expected
        assert si.digest_algorithm == DigestAlgorithm.SHA256

    def test_from_inventory_camel_case_keys(self):
        """JSON output uses camelCase keys."""
        inv = msgspec.json.decode(_inv_json(contentDirectory="stuff"), type=Inventory)
        si = StoredInventory.from_inventory(inv, DigestAlgorithm.SHA512)
        parsed = json.loads(si.raw_bytes)
        assert "digestAlgorithm" in parsed
        assert "contentDirectory" in parsed
        assert "digest_algorithm" not in parsed

    def test_sidecar_filename(self):
        si = StoredInventory.from_bytes(_inv_json(), DigestAlgorithm.SHA512)
        assert si.sidecar_filename == "inventory.json.sha512"

    def test_sidecar_filename_sha256(self):
        si = StoredInventory.from_bytes(_inv_json(), DigestAlgorithm.SHA256)
        assert si.sidecar_filename == "inventory.json.sha256"

    def test_sidecar_content(self):
        si = StoredInventory.from_bytes(_inv_json(), DigestAlgorithm.SHA512)
        expected = f"{si.digest} inventory.json\n"
        assert si.sidecar_content == expected

    def test_byte_preservation_from_fixture(self):
        """Bytes read from disk are preserved exactly through StoredInventory."""
        fixture = (
            pathlib.Path(__file__).parents[3]
            / "tests"
            / "fixtures"
            / "1.1"
            / "good-objects"
            / "minimal_no_content"
            / "inventory.json"
        )
        if not fixture.exists():
            pytest.skip("fixtures not available")
        raw = fixture.read_bytes()
        si = StoredInventory.from_bytes(raw, DigestAlgorithm.SHA512)
        assert si.raw_bytes == raw

    def test_from_inventory_omits_none_fields(self):
        """Optional None fields not in JSON output."""
        inv = msgspec.json.decode(_inv_json(), type=Inventory)
        si = StoredInventory.from_inventory(inv, DigestAlgorithm.SHA512)
        parsed = json.loads(si.raw_bytes)
        assert "contentDirectory" not in parsed
        assert "fixity" not in parsed
