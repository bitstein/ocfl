"""Tests for StagingStore."""

import hashlib
import os

import pytest

from ocfl_cli.staging import StagingStore
from ocfl_lib import DigestAlgorithm, Spec
from ocfl_lib.inventory import Inventory
from ocfl_lib.inventory_builder import InventoryBuilder


@pytest.fixture
def store(tmp_path):
    return StagingStore(tmp_path)


@pytest.fixture
def sample_inventory() -> Inventory:
    return (
        InventoryBuilder()
        .id("obj:1")
        .spec(Spec.V1_1)
        .digest_algorithm(DigestAlgorithm.SHA512)
        .add_version({"abc123": ["file.txt"]}, created="2024-01-01T00:00:00Z")
        .build()
    )


class TestStagingPath:
    def test_deterministic(self, store):
        p1 = store.staging_path("/root", "obj:1")
        p2 = store.staging_path("/root", "obj:1")
        assert p1 == p2

    def test_different_root_different_path(self, store):
        p1 = store.staging_path("/root-a", "obj:1")
        p2 = store.staging_path("/root-b", "obj:1")
        assert p1 != p2

    def test_different_id_different_path(self, store):
        p1 = store.staging_path("/root", "obj:1")
        p2 = store.staging_path("/root", "obj:2")
        assert p1 != p2

    def test_uses_sha256_with_null_separator(self, store, tmp_path):
        expected = hashlib.sha256(b"/root\x00obj:1").hexdigest()
        assert store.staging_path("/root", "obj:1") == tmp_path / expected


class TestExists:
    def test_not_exists(self, store):
        assert not store.exists("/root", "obj:1")

    def test_exists_after_create(self, store, sample_inventory):
        store.create("/root", "obj:1", sample_inventory)
        assert store.exists("/root", "obj:1")


class TestInventoryRoundTrip:
    def test_create_and_load(self, store, sample_inventory):
        store.create("/root", "obj:1", sample_inventory)
        loaded = store.load_inventory("/root", "obj:1")
        assert loaded.id == sample_inventory.id
        assert loaded.head == sample_inventory.head
        assert loaded.manifest == sample_inventory.manifest

    def test_save_and_load(self, store, sample_inventory):
        store.create("/root", "obj:1", sample_inventory)
        # Modify and save
        new_inv = (
            InventoryBuilder(sample_inventory)
            .add_version({"def456": ["file2.txt"]}, created="2024-02-01T00:00:00Z")
            .build()
        )
        store.save_inventory("/root", "obj:1", new_inv)
        loaded = store.load_inventory("/root", "obj:1")
        assert loaded.head == "v2"
        assert "def456" in loaded.manifest


class TestContentMapRoundTrip:
    def test_empty_content_map_on_create(self, store, sample_inventory):
        store.create("/root", "obj:1", sample_inventory)
        cm = store.load_content_map("/root", "obj:1")
        assert cm == {}

    def test_save_and_load(self, store, sample_inventory):
        store.create("/root", "obj:1", sample_inventory)
        content_map = {
            "abc123": {"path": "/home/user/file.txt", "size": 42, "mtime": 1700000000.0}
        }
        store.save_content_map("/root", "obj:1", content_map)
        loaded = store.load_content_map("/root", "obj:1")
        assert loaded == content_map


class TestPurge:
    def test_purge_removes_staging(self, store, sample_inventory):
        store.create("/root", "obj:1", sample_inventory)
        assert store.exists("/root", "obj:1")
        store.purge("/root", "obj:1")
        assert not store.exists("/root", "obj:1")

    def test_purge_nonexistent_is_noop(self, store):
        # Should not raise
        store.purge("/root", "obj:1")


class TestAtomicContentMap:
    """content.json writes must be atomic (write-temp + fsync + os.replace)."""

    def test_save_content_map_no_temp_files_left(self, store, sample_inventory):
        store.create("/root", "obj:1", sample_inventory)
        content_map = {
            "abc123": {"path": "/src/file.txt", "size": 1, "mtime": 1700000000.0}
        }
        store.save_content_map("/root", "obj:1", content_map)

        d = store.staging_path("/root", "obj:1")
        names = sorted(p.name for p in d.iterdir())
        assert names == ["content.json", "inventory.json"]

    def test_save_content_map_is_atomic_under_crash(
        self, store, sample_inventory, monkeypatch
    ):
        store.create("/root", "obj:1", sample_inventory)
        map_a = {"aaa": {"path": "/src/a.txt", "size": 1, "mtime": 1700000000.0}}
        store.save_content_map("/root", "obj:1", map_a)

        map_b = {"bbb": {"path": "/src/b.txt", "size": 2, "mtime": 1700000001.0}}

        real_replace = os.replace

        def boom(src, dst, *args, **kwargs):
            if str(dst).endswith("content.json"):
                raise OSError("boom")
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "replace", boom)

        with pytest.raises(OSError, match="boom"):
            store.save_content_map("/root", "obj:1", map_b)

        assert store.load_content_map("/root", "obj:1") == map_a
        d = store.staging_path("/root", "obj:1")
        leftover_tmps = [
            p for p in d.iterdir() if p.name != "content.json" and ".tmp" in p.name
        ]
        assert leftover_tmps == []
