"""Tests for StagingStore."""

import hashlib
import os

import pytest

from ocfl_cli.errors import StagingError
from ocfl_cli.stage.schemas import StagedMetadata, StagedUser
from ocfl_cli.stage.store import StagingStore
from ocfl_lib import DigestAlgorithm, Inventory, InventoryBuilder, Spec


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


class TestMetadataRoundTrip:
    def test_empty_metadata_on_create(self, store, sample_inventory):
        store.create("/root", "obj:1", sample_inventory)

        metadata = store.load_metadata("/root", "obj:1")

        assert metadata == StagedMetadata()

    def test_missing_metadata_file_loads_empty(self, store, sample_inventory):
        store.create("/root", "obj:1", sample_inventory)
        d = store.staging_path("/root", "obj:1")
        (d / "metadata.json").unlink()

        metadata = store.load_metadata("/root", "obj:1")

        assert metadata == StagedMetadata()

    def test_save_and_load(self, store, sample_inventory):
        store.create("/root", "obj:1", sample_inventory)
        metadata = StagedMetadata(
            message="initial deposit",
            user=StagedUser(name="Alice", address="mailto:alice@example.org"),
        )

        store.save_metadata("/root", "obj:1", metadata)

        assert store.load_metadata("/root", "obj:1") == metadata

    @pytest.mark.parametrize(
        "metadata",
        [
            StagedMetadata(message=""),
            StagedMetadata(message="   "),
            StagedMetadata(user=StagedUser(name="")),
            StagedMetadata(user=StagedUser(name="\t")),
            StagedMetadata(user=StagedUser(address="")),
            StagedMetadata(user=StagedUser(address="\n")),
        ],
    )
    def test_save_rejects_empty_or_whitespace_values(
        self, store, sample_inventory, metadata
    ):
        store.create("/root", "obj:1", sample_inventory)

        with pytest.raises(StagingError, match="metadata"):
            store.save_metadata("/root", "obj:1", metadata)

    def test_load_rejects_empty_or_whitespace_values(self, store, sample_inventory):
        store.create("/root", "obj:1", sample_inventory)
        d = store.staging_path("/root", "obj:1")
        (d / "metadata.json").write_text('{"message":" "}')

        with pytest.raises(StagingError, match="message"):
            store.load_metadata("/root", "obj:1")

    @pytest.mark.parametrize(
        ("raw", "match"),
        [
            ('{"message":"initial","created":"2024-01-01T00:00:00Z"}', "created"),
            ('{"user":{"name":"Alice","email":"alice@example.org"}}', "email"),
        ],
    )
    def test_load_rejects_unknown_fields(self, store, sample_inventory, raw, match):
        store.create("/root", "obj:1", sample_inventory)
        d = store.staging_path("/root", "obj:1")
        (d / "metadata.json").write_text(raw)

        with pytest.raises(StagingError, match=match):
            store.load_metadata("/root", "obj:1")

    @pytest.mark.parametrize(
        "raw",
        [
            "[]",
            '{"message":["initial"]}',
            '{"user":"Alice"}',
            '{"user":{"name":["Alice"]}}',
        ],
    )
    def test_load_rejects_malformed_metadata_shape(self, store, sample_inventory, raw):
        store.create("/root", "obj:1", sample_inventory)
        d = store.staging_path("/root", "obj:1")
        (d / "metadata.json").write_text(raw)

        with pytest.raises(StagingError):
            store.load_metadata("/root", "obj:1")


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
        assert names == ["content.json", "inventory.json", "metadata.json"]

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
