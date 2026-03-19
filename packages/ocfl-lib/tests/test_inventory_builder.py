"""Tests for InventoryBuilder (Phase 4.4)."""

from typing import TYPE_CHECKING

import pytest

from ocfl_lib.digest import DigestAlgorithm
from ocfl_lib.inventory import Inventory, UserInfo
from ocfl_lib.inventory_builder import InventoryBuilder
from ocfl_lib.spec import Spec

if TYPE_CHECKING:
    from ocfl_lib.digest_map import DigestMap


class TestBuildFirstVersion:
    def test_minimal(self):
        state: DigestMap = {"abc123": ["file.txt"]}
        inv = (
            InventoryBuilder()
            .id("http://example.org/obj1")
            .add_version(state, created="2024-01-01T00:00:00Z")
            .build()
        )
        assert inv.id == "http://example.org/obj1"
        assert inv.type == Spec.V1_1.inventory_type_url
        assert inv.digest_algorithm == "sha512"
        assert inv.head == "v1"
        assert "v1" in inv.versions
        assert inv.versions["v1"].created == "2024-01-01T00:00:00Z"
        assert inv.versions["v1"].state == state

    def test_manifest_generated(self):
        """Content paths generated as v1/content/<logical_path>."""
        state: DigestMap = {"abc": ["dir/file.txt"], "def": ["other.txt"]}
        inv = (
            InventoryBuilder()
            .id("test")
            .add_version(state, created="2024-01-01T00:00:00Z")
            .build()
        )
        # Each digest should map to v1/content/<logical_path>
        assert inv.manifest["abc"] == ["v1/content/dir/file.txt"]
        assert inv.manifest["def"] == ["v1/content/other.txt"]

    def test_custom_content_directory(self):
        state: DigestMap = {"abc": ["file.txt"]}
        inv = (
            InventoryBuilder()
            .id("test")
            .content_directory("stuff")
            .add_version(state, created="2024-01-01T00:00:00Z")
            .build()
        )
        assert inv.content_directory == "stuff"
        assert inv.manifest["abc"] == ["v1/stuff/file.txt"]

    def test_spec_v1_0(self):
        state: DigestMap = {"abc": ["file.txt"]}
        inv = (
            InventoryBuilder()
            .id("test")
            .spec(Spec.V1_0)
            .add_version(state, created="2024-01-01T00:00:00Z")
            .build()
        )
        assert inv.type == Spec.V1_0.inventory_type_url

    def test_sha256(self):
        state: DigestMap = {"abc": ["file.txt"]}
        inv = (
            InventoryBuilder()
            .id("test")
            .digest_algorithm(DigestAlgorithm.SHA256)
            .add_version(state, created="2024-01-01T00:00:00Z")
            .build()
        )
        assert inv.digest_algorithm == "sha256"

    def test_version_message_and_user(self):
        state: DigestMap = {"abc": ["file.txt"]}
        user = UserInfo(name="Alice", address="mailto:alice@example.org")
        inv = (
            InventoryBuilder()
            .id("test")
            .add_version(
                state,
                created="2024-01-01T00:00:00Z",
                message="initial deposit",
                user=user,
            )
            .build()
        )
        v1 = inv.versions["v1"]
        assert v1.message == "initial deposit"
        assert v1.user == user

    def test_empty_state(self):
        inv = (
            InventoryBuilder()
            .id("test")
            .add_version({}, created="2024-01-01T00:00:00Z")
            .build()
        )
        assert inv.manifest == {}
        assert inv.versions["v1"].state == {}

    def test_multiple_paths_same_digest(self):
        """Deduplication: same content at multiple logical paths."""
        state: DigestMap = {"abc": ["a.txt", "copy.txt"]}
        inv = (
            InventoryBuilder()
            .id("test")
            .add_version(state, created="2024-01-01T00:00:00Z")
            .build()
        )
        # Only one content path in manifest (first logical path)
        assert len(inv.manifest["abc"]) == 1
        assert inv.manifest["abc"][0].startswith("v1/content/")


class TestBuildSubsequentVersion:
    def _base_inv(self) -> Inventory:
        state: DigestMap = {"abc": ["file.txt"]}
        return (
            InventoryBuilder()
            .id("test")
            .add_version(state, created="2024-01-01T00:00:00Z")
            .build()
        )

    def test_adds_v2(self):
        prev = self._base_inv()
        new_state: DigestMap = {"abc": ["file.txt"], "def": ["new.txt"]}
        inv = (
            InventoryBuilder(prev=prev)
            .add_version(new_state, created="2024-06-01T00:00:00Z")
            .build()
        )
        assert inv.head == "v2"
        assert len(inv.versions) == 2
        assert inv.versions["v1"] == prev.versions["v1"]
        assert inv.versions["v2"].state == new_state

    def test_forward_delta_manifest(self):
        """Existing content reuses manifest entries; only new content added."""
        prev = self._base_inv()
        new_state: DigestMap = {"abc": ["file.txt"], "def": ["new.txt"]}
        inv = (
            InventoryBuilder(prev=prev)
            .add_version(new_state, created="2024-06-01T00:00:00Z")
            .build()
        )
        # abc should still point to v1 content path
        assert inv.manifest["abc"] == ["v1/content/file.txt"]
        # def is new, should point to v2
        assert inv.manifest["def"] == ["v2/content/new.txt"]

    def test_preserves_prior_versions(self):
        prev = self._base_inv()
        inv = (
            InventoryBuilder(prev=prev)
            .add_version({"abc": ["file.txt"]}, created="2024-06-01T00:00:00Z")
            .build()
        )
        assert inv.versions["v1"] == prev.versions["v1"]

    def test_preserves_id_and_settings(self):
        prev = self._base_inv()
        inv = (
            InventoryBuilder(prev=prev)
            .add_version({"abc": ["file.txt"]}, created="2024-06-01T00:00:00Z")
            .build()
        )
        assert inv.id == prev.id
        assert inv.type == prev.type
        assert inv.digest_algorithm == prev.digest_algorithm


class TestContentPathFunc:
    def test_custom_content_path(self):
        state: DigestMap = {"abc": ["file.txt"]}
        inv = (
            InventoryBuilder()
            .id("test")
            .content_path_func(lambda logical: logical.upper())
            .add_version(state, created="2024-01-01T00:00:00Z")
            .build()
        )
        assert inv.manifest["abc"] == ["v1/content/FILE.TXT"]


class TestValidationFailures:
    def test_no_id(self):
        with pytest.raises(ValueError, match="id"):
            InventoryBuilder().add_version({}, created="2024-01-01T00:00:00Z").build()

    def test_no_versions(self):
        with pytest.raises(ValueError, match="version"):
            InventoryBuilder().id("test").build()
