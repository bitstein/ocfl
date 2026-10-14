from typing import TYPE_CHECKING

import pytest

from ocfl_lib import (
    DigestAlgorithm,
    LocalFS,
    Object,
    Spec,
    StorageRoot,
    VersionState,
    VNum,
    bytes_digest,
    get_layout,
    scan_dir,
)
from ocfl_lib.validation import validate_object

if TYPE_CHECKING:
    from pathlib import Path


def _make_src_dir(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create a source directory with given files."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    for name, content in files.items():
        p = src / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return src


# Flat-direct layout: object "obj" lives at tmp_path / "obj".
OID = "obj"


def _init_root(tmp_path: Path) -> LocalFS:
    fs = LocalFS(tmp_path)
    layout = get_layout("0002-flat-direct-storage-layout")
    StorageRoot.init(fs, fs, spec=Spec.V1_1, layout=layout)
    return fs


def _commit(fs: LocalFS, stage: VersionState, **kw) -> Object:
    return StorageRoot.open(fs).commit(fs, OID, stage, **kw)


def _create_v1(
    tmp_path: Path,
    files: dict[str, str],
    alg: DigestAlgorithm = DigestAlgorithm.SHA256,
) -> tuple[Object, LocalFS]:
    """Helper: create v1 object with given files and algorithm."""
    src = _make_src_dir(tmp_path, files)
    stage = scan_dir(LocalFS(src.parent), "src", alg)
    fs = _init_root(tmp_path)
    obj = _commit(fs, stage, spec=Spec.V1_1, created="2024-01-01T00:00:00Z")
    return obj, fs


class TestAlgorithmMigration:
    def test_sha256_to_sha512(self, tmp_path: Path):
        """Create v1 sha256, v2 sha512 — fixity has sha256 digests."""
        obj, obj_fs = _create_v1(tmp_path, {"file.txt": "hello"})
        assert obj.inventory.inventory.digest_algorithm == "sha256"

        src2 = tmp_path / "src2"
        src2.mkdir()
        (src2 / "file.txt").write_text("hello")
        (src2 / "new.txt").write_text("world")

        stage2 = scan_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)
        obj = _commit(obj_fs, stage2, created="2024-06-01T00:00:00Z")

        inv = obj.inventory.inventory
        assert inv.digest_algorithm == "sha512"
        assert inv.fixity is not None
        assert "sha256" in inv.fixity

        result = validate_object(obj_fs, OID)
        assert result.is_valid, f"errors: {[e.render() for e in result.errors]}"

    def test_version_states_remapped(self, tmp_path: Path):
        obj, obj_fs = _create_v1(tmp_path, {"a.txt": "aaa"})

        src2 = tmp_path / "src2"
        src2.mkdir()
        (src2 / "a.txt").write_text("bbb")

        stage2 = scan_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)
        obj = _commit(obj_fs, stage2, created="2024-06-01T00:00:00Z")

        inv = obj.inventory.inventory
        for digest in inv.versions["v1"].state:
            assert len(digest) == 128, f"v1 digest not sha512: {digest[:16]}..."
        for digest in inv.versions["v2"].state:
            assert len(digest) == 128, f"v2 digest not sha512: {digest[:16]}..."
        for digest in inv.manifest:
            assert len(digest) == 128, f"manifest digest not sha512: {digest[:16]}..."

    def test_content_files_unchanged(self, tmp_path: Path):
        obj, obj_fs = _create_v1(tmp_path, {"file.txt": "hello"})
        obj_dir = tmp_path / "obj"

        inv1 = obj.inventory.inventory
        v1_contents = {}
        for paths in inv1.manifest.values():
            for cp in paths:
                v1_contents[cp] = (obj_dir / cp).read_bytes()

        src2 = tmp_path / "src2"
        src2.mkdir()
        (src2 / "file.txt").write_text("hello")
        (src2 / "extra.txt").write_text("extra")

        stage2 = scan_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)
        obj = _commit(obj_fs, stage2, created="2024-06-01T00:00:00Z")

        for cp, content in v1_contents.items():
            assert (obj_dir / cp).read_bytes() == content

    def test_read_back_after_migration(self, tmp_path: Path):
        obj, obj_fs = _create_v1(tmp_path, {"file.txt": "hello"})

        src2 = tmp_path / "src2"
        src2.mkdir()
        (src2 / "file.txt").write_text("hello")
        (src2 / "new.txt").write_text("world")

        stage2 = scan_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)
        obj = _commit(obj_fs, stage2, created="2024-06-01T00:00:00Z")

        with obj.open_logical_path(VNum(1), "file.txt") as f:
            assert f.read() == b"hello"
        with obj.open_logical_path(VNum(2), "file.txt") as f:
            assert f.read() == b"hello"
        with obj.open_logical_path(VNum(2), "new.txt") as f:
            assert f.read() == b"world"

    def test_old_root_sidecar_removed(self, tmp_path: Path):
        _obj, obj_fs = _create_v1(tmp_path, {"file.txt": "hello"})
        obj_dir = tmp_path / "obj"
        assert (obj_dir / "inventory.json.sha256").exists()

        src2 = tmp_path / "src2"
        src2.mkdir()
        (src2 / "file.txt").write_text("hello")

        stage2 = scan_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)
        _commit(obj_fs, stage2, created="2024-06-01T00:00:00Z")

        assert not (obj_dir / "inventory.json.sha256").exists()
        assert (obj_dir / "inventory.json.sha512").exists()

    def test_fixity_preserves_old_digests(self, tmp_path: Path):
        obj, obj_fs = _create_v1(tmp_path, {"file.txt": "hello"})
        old_manifest = dict(obj.inventory.inventory.manifest)

        src2 = tmp_path / "src2"
        src2.mkdir()
        (src2 / "file.txt").write_text("hello")

        stage2 = scan_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)
        obj = _commit(obj_fs, stage2, created="2024-06-01T00:00:00Z")

        inv = obj.inventory.inventory
        assert inv.fixity is not None
        sha256_fixity = inv.fixity["sha256"]
        for old_digest, old_paths in old_manifest.items():
            assert old_digest in sha256_fixity
            assert sha256_fixity[old_digest] == old_paths

    def test_dedup_across_migration(self, tmp_path: Path):
        obj, obj_fs = _create_v1(tmp_path, {"file.txt": "same content"})

        src2 = tmp_path / "src2"
        src2.mkdir()
        (src2 / "file.txt").write_text("same content")

        stage2 = scan_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)
        obj = _commit(obj_fs, stage2, created="2024-06-01T00:00:00Z")

        inv = obj.inventory.inventory
        v2_paths = [
            p for ps in inv.manifest.values() for p in ps if p.startswith("v2/")
        ]
        assert len(v2_paths) == 0

    def test_two_step_migration(self, tmp_path: Path):
        obj, obj_fs = _create_v1(tmp_path, {"file.txt": "hello"})

        src2 = tmp_path / "src2"
        src2.mkdir()
        (src2 / "file.txt").write_text("hello")
        (src2 / "new.txt").write_text("world")

        stage2 = scan_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)
        root = StorageRoot.open(obj_fs)
        plan = root.plan(OID, stage2, created="2024-06-01T00:00:00Z")
        obj = root.commit(obj_fs, OID, plan, content=stage2.content_source)

        assert obj.inventory.inventory.digest_algorithm == "sha512"

        result = validate_object(obj_fs, OID)
        assert result.is_valid, f"errors: {[e.render() for e in result.errors]}"

    def test_existing_fixity_preserved(self, tmp_path: Path):
        src = _make_src_dir(tmp_path, {"file.txt": "hello"})
        stage1 = scan_dir(
            LocalFS(src.parent),
            "src",
            DigestAlgorithm.SHA256,
            fixity_algs=[DigestAlgorithm.MD5],
        )
        obj_fs = _init_root(tmp_path)
        obj = _commit(obj_fs, stage1, spec=Spec.V1_1, created="2024-01-01T00:00:00Z")

        assert obj.inventory.inventory.fixity is not None
        assert "md5" in obj.inventory.inventory.fixity

        src2 = tmp_path / "src2"
        src2.mkdir()
        (src2 / "file.txt").write_text("hello")

        stage2 = scan_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)
        obj = _commit(obj_fs, stage2, created="2024-06-01T00:00:00Z")

        inv = obj.inventory.inventory
        assert inv.fixity is not None
        assert "md5" in inv.fixity
        assert "sha256" in inv.fixity

        result = validate_object(obj_fs, OID)
        assert result.is_valid, f"errors: {[e.render() for e in result.errors]}"

    def test_migration_error_reverts(self, tmp_path: Path):
        _obj, obj_fs = _create_v1(tmp_path, {"file.txt": "hello"})

        new_digest = bytes_digest(b"brand_new", DigestAlgorithm.SHA512)

        class FailingSource:
            def get_content(self, digest: str):
                raise OSError("disk full")

        sha512_digest = bytes_digest(b"hello", DigestAlgorithm.SHA512)
        stage = VersionState(
            state={
                sha512_digest: ["file.txt"],
                new_digest: ["new.txt"],
            },
            digest_algorithm=DigestAlgorithm.SHA512,
            content_source=FailingSource(),
        )

        with pytest.raises(OSError, match="disk full"):
            _commit(obj_fs, stage, created="2024-06-01T00:00:00Z")

        obj_dir = tmp_path / "obj"
        assert not (obj_dir / "v2").exists()

        original_inv_bytes = (obj_dir / "v1" / "inventory.json").read_bytes()
        assert (obj_dir / "inventory.json").read_bytes() == original_inv_bytes

        assert (obj_dir / "inventory.json.sha256").is_file()
        assert not (obj_dir / "inventory.json.sha512").exists()

        result = validate_object(obj_fs, OID)
        assert result.is_valid, result.errors

    def test_migration_revert_restores_original_inventory(self, tmp_path: Path):
        _obj, obj_fs = _create_v1(tmp_path, {"file.txt": "hello"})
        obj_dir = tmp_path / "obj"
        orig_inv_bytes = (obj_dir / "inventory.json").read_bytes()

        src2 = tmp_path / "src2"
        src2.mkdir()
        (src2 / "file.txt").write_text("hello")
        stage = scan_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)

        class FailRootInventory(LocalFS):
            def write(self, name, data):
                if name == f"{OID}/inventory.json":
                    raise RuntimeError("injected")
                return super().write(name, data)

        with pytest.raises(RuntimeError, match="injected"):
            StorageRoot.open(obj_fs).commit(
                FailRootInventory(tmp_path), OID, stage, created="2024-06-01T00:00:00Z"
            )

        assert (obj_dir / "inventory.json").read_bytes() == orig_inv_bytes
        assert (obj_dir / "inventory.json.sha256").is_file()
        assert not (obj_dir / "inventory.json.sha512").exists()
        assert not (obj_dir / "v2").exists()
