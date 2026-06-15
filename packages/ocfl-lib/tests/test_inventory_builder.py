"""Tests for InventoryBuilder."""

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


class TestFixitySupport:
    """InventoryBuilder fixity support."""

    def test_fixity_populated_from_source(self):
        """New manifest digests query FixitySource, populate fixity block."""
        state: DigestMap = {"abc123": ["file.txt"]}

        class FakeFixity:
            def get_fixity(self, digest: str) -> dict[DigestAlgorithm, str]:
                if digest == "abc123":
                    return {DigestAlgorithm.MD5: "md5_of_abc123"}
                return {}

        inv = (
            InventoryBuilder()
            .id("test")
            .fixity_source(FakeFixity())
            .add_version(state, created="2024-01-01T00:00:00Z")
            .build()
        )
        assert inv.fixity is not None
        assert "md5" in inv.fixity
        assert "md5_of_abc123" in inv.fixity["md5"]
        # fixity content path should match manifest content path
        assert inv.fixity["md5"]["md5_of_abc123"] == inv.manifest["abc123"]

    def test_no_fixity_when_source_is_none(self):
        state: DigestMap = {"abc": ["file.txt"]}
        inv = (
            InventoryBuilder()
            .id("test")
            .add_version(state, created="2024-01-01T00:00:00Z")
            .build()
        )
        assert inv.fixity is None

    def test_fixity_multiple_algorithms(self):
        """FixitySource returning multiple algs populates all."""
        state: DigestMap = {"abc": ["file.txt"]}

        class MultiFixity:
            def get_fixity(self, digest: str) -> dict[DigestAlgorithm, str]:
                return {
                    DigestAlgorithm.MD5: "md5val",
                    DigestAlgorithm.SHA1: "sha1val",
                }

        inv = (
            InventoryBuilder()
            .id("test")
            .fixity_source(MultiFixity())
            .add_version(state, created="2024-01-01T00:00:00Z")
            .build()
        )
        assert inv.fixity is not None
        assert "md5" in inv.fixity
        assert "sha1" in inv.fixity

    def test_fixity_collision_keeps_all_paths(self):
        state: DigestMap = {"abc": ["a.txt"], "def": ["b.txt"]}

        class CollidingFixity:
            def get_fixity(self, digest: str) -> dict[DigestAlgorithm, str]:
                return {DigestAlgorithm.MD5: "same_md5"}

        inv = (
            InventoryBuilder()
            .id("test")
            .fixity_source(CollidingFixity())
            .add_version(state, created="2024-01-01T00:00:00Z")
            .build()
        )

        assert inv.fixity is not None
        assert sorted(inv.fixity["md5"]["same_md5"]) == [
            "v1/content/a.txt",
            "v1/content/b.txt",
        ]

    def test_fixity_merged_with_prev(self):
        """Fixity from prev inventory preserved, new fixity added."""
        # Build v1 with fixity
        state_v1: DigestMap = {"abc": ["file.txt"]}

        class FixityV1:
            def get_fixity(self, digest: str) -> dict[DigestAlgorithm, str]:
                if digest == "abc":
                    return {DigestAlgorithm.MD5: "md5_abc"}
                return {}

        prev = (
            InventoryBuilder()
            .id("test")
            .fixity_source(FixityV1())
            .add_version(state_v1, created="2024-01-01T00:00:00Z")
            .build()
        )
        assert prev.fixity is not None

        # Build v2 with new file and fixity
        state_v2: DigestMap = {"abc": ["file.txt"], "def": ["new.txt"]}

        class FixityV2:
            def get_fixity(self, digest: str) -> dict[DigestAlgorithm, str]:
                if digest == "def":
                    return {DigestAlgorithm.MD5: "md5_def"}
                return {}

        inv = (
            InventoryBuilder(prev=prev)
            .fixity_source(FixityV2())
            .add_version(state_v2, created="2024-06-01T00:00:00Z")
            .build()
        )
        assert inv.fixity is not None
        # Both old and new fixity present
        md5 = inv.fixity["md5"]
        assert "md5_abc" in md5
        assert "md5_def" in md5

    def test_fixity_not_queried_for_existing_digests(self):
        """Existing manifest digests (dedup) should not query fixity again."""
        state_v1: DigestMap = {"abc": ["file.txt"]}
        prev = (
            InventoryBuilder()
            .id("test")
            .add_version(state_v1, created="2024-01-01T00:00:00Z")
            .build()
        )

        calls: list[str] = []

        class TrackingFixity:
            def get_fixity(self, digest: str) -> dict[DigestAlgorithm, str]:
                calls.append(digest)
                return {DigestAlgorithm.MD5: f"md5_{digest}"}

        state_v2: DigestMap = {"abc": ["file.txt"], "def": ["new.txt"]}
        InventoryBuilder(prev=prev).fixity_source(TrackingFixity()).add_version(
            state_v2, created="2024-06-01T00:00:00Z"
        ).build()
        # Only "def" should be queried (abc already in manifest)
        assert calls == ["def"]

    def test_fixity_empty_source_returns(self):
        """FixitySource returning empty dict → no fixity block."""
        state: DigestMap = {"abc": ["file.txt"]}

        class EmptyFixity:
            def get_fixity(self, digest: str) -> dict[DigestAlgorithm, str]:
                return {}

        inv = (
            InventoryBuilder()
            .id("test")
            .fixity_source(EmptyFixity())
            .add_version(state, created="2024-01-01T00:00:00Z")
            .build()
        )
        assert inv.fixity is None


class TestValidationFailures:
    def test_no_id(self):
        with pytest.raises(ValueError, match="id"):
            InventoryBuilder().add_version({}, created="2024-01-01T00:00:00Z").build()

    def test_no_versions(self):
        with pytest.raises(ValueError, match="version"):
            InventoryBuilder().id("test").build()

    def test_build_twice_raises(self):
        state: DigestMap = {"abc123": ["file.txt"]}
        b = (
            InventoryBuilder()
            .id("http://example.org/obj")
            .add_version(state, created="2024-01-01T00:00:00Z")
        )
        b.build()
        with pytest.raises(ValueError, match="already"):
            b.build()


class TestMixedCaseDedupe:
    def test_mixed_case_digest_dedupes(self):
        """Same digest in different case across versions reuses content path."""
        state1: DigestMap = {"ABC": ["file.txt"]}
        inv1 = (
            InventoryBuilder()
            .id("test")
            .add_version(state1, created="2024-01-01T00:00:00Z")
            .build()
        )
        assert "ABC" in inv1.manifest

        state2: DigestMap = {"abc": ["file2.txt"]}
        inv2 = (
            InventoryBuilder(prev=inv1)
            .add_version(state2, created="2024-02-01T00:00:00Z")
            .build()
        )
        # Should reuse v1 manifest entry, no new content path
        assert len(inv2.manifest) == 1
