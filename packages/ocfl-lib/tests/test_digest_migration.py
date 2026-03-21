"""Tests for digest algorithm migration."""

from typing import TYPE_CHECKING

from ocfl_lib.digest import DigestAlgorithm
from ocfl_lib.fs.local import LocalFS
from ocfl_lib.object import Object
from ocfl_lib.spec import Spec, VNum
from ocfl_lib.stage import stage_dir
from ocfl_lib.validation.object import validate_object
from ocfl_lib.validation.result import ValidationResult

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


def _create_v1(
    tmp_path: Path,
    files: dict[str, str],
    alg: DigestAlgorithm = DigestAlgorithm.SHA256,
) -> tuple[Object, LocalFS]:
    """Helper: create v1 object with given files and algorithm."""
    src = _make_src_dir(tmp_path, files)
    obj_dir = tmp_path / "obj"
    obj_dir.mkdir()

    src_fs = LocalFS(src.parent)
    stage = stage_dir(src_fs, "src", alg)

    obj_fs = LocalFS(obj_dir)
    obj = Object(obj_fs, "")
    obj.update(
        obj_fs,
        stage,
        id="test:migration",
        spec=Spec.V1_1,
        created="2024-01-01T00:00:00Z",
    )
    return obj, obj_fs


class TestAlgorithmMigration:
    """Algorithm migration between versions."""

    def test_sha256_to_sha512(self, tmp_path: Path):
        """Create v1 sha256, v2 sha512 — fixity has sha256 digests."""
        obj, obj_fs = _create_v1(tmp_path, {"file.txt": "hello"})
        assert obj.inventory.inventory.digest_algorithm == "sha256"

        src2 = tmp_path / "src2"
        src2.mkdir()
        (src2 / "file.txt").write_text("hello")
        (src2 / "new.txt").write_text("world")

        stage2 = stage_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)
        obj.update(obj_fs, stage2, created="2024-06-01T00:00:00Z")

        inv = obj.inventory.inventory
        assert inv.digest_algorithm == "sha512"
        assert inv.fixity is not None
        assert "sha256" in inv.fixity

        result = ValidationResult()
        validate_object(obj_fs, "", result)
        assert result.is_valid, f"errors: {[e.message for e in result.errors]}"

    def test_version_states_remapped(self, tmp_path: Path):
        """All version states use new algorithm digests after migration."""
        obj, obj_fs = _create_v1(tmp_path, {"a.txt": "aaa"})

        src2 = tmp_path / "src2"
        src2.mkdir()
        (src2 / "a.txt").write_text("bbb")

        stage2 = stage_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)
        obj.update(obj_fs, stage2, created="2024-06-01T00:00:00Z")

        inv = obj.inventory.inventory
        for digest in inv.versions["v1"].state:
            assert len(digest) == 128, f"v1 digest not sha512: {digest[:16]}..."
        for digest in inv.versions["v2"].state:
            assert len(digest) == 128, f"v2 digest not sha512: {digest[:16]}..."
        for digest in inv.manifest:
            assert len(digest) == 128, f"manifest digest not sha512: {digest[:16]}..."

    def test_content_files_unchanged(self, tmp_path: Path):
        """Content files on disk not modified during migration."""
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

        stage2 = stage_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)
        obj.update(obj_fs, stage2, created="2024-06-01T00:00:00Z")

        for cp, content in v1_contents.items():
            assert (obj_dir / cp).read_bytes() == content

    def test_read_back_after_migration(self, tmp_path: Path):
        """Content accessible via logical paths after migration."""
        obj, obj_fs = _create_v1(tmp_path, {"file.txt": "hello"})

        src2 = tmp_path / "src2"
        src2.mkdir()
        (src2 / "file.txt").write_text("hello")
        (src2 / "new.txt").write_text("world")

        stage2 = stage_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)
        obj.update(obj_fs, stage2, created="2024-06-01T00:00:00Z")

        with obj.open_logical_path(VNum(1), "file.txt") as f:
            assert f.read() == b"hello"
        with obj.open_logical_path(VNum(2), "file.txt") as f:
            assert f.read() == b"hello"
        with obj.open_logical_path(VNum(2), "new.txt") as f:
            assert f.read() == b"world"

    def test_old_root_sidecar_removed(self, tmp_path: Path):
        """Old root sidecar removed after migration."""
        obj, obj_fs = _create_v1(tmp_path, {"file.txt": "hello"})
        obj_dir = tmp_path / "obj"
        assert (obj_dir / "inventory.json.sha256").exists()

        src2 = tmp_path / "src2"
        src2.mkdir()
        (src2 / "file.txt").write_text("hello")

        stage2 = stage_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)
        obj.update(obj_fs, stage2, created="2024-06-01T00:00:00Z")

        assert not (obj_dir / "inventory.json.sha256").exists()
        assert (obj_dir / "inventory.json.sha512").exists()

    def test_fixity_preserves_old_digests(self, tmp_path: Path):
        """Fixity maps old digest → same content paths as old manifest."""
        obj, obj_fs = _create_v1(tmp_path, {"file.txt": "hello"})
        old_manifest = dict(obj.inventory.inventory.manifest)

        src2 = tmp_path / "src2"
        src2.mkdir()
        (src2 / "file.txt").write_text("hello")

        stage2 = stage_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)
        obj.update(obj_fs, stage2, created="2024-06-01T00:00:00Z")

        inv = obj.inventory.inventory
        assert inv.fixity is not None
        sha256_fixity = inv.fixity["sha256"]
        for old_digest, old_paths in old_manifest.items():
            assert old_digest in sha256_fixity
            assert sha256_fixity[old_digest] == old_paths

    def test_dedup_across_migration(self, tmp_path: Path):
        """Unchanged files deduplicated across algorithm change."""
        obj, obj_fs = _create_v1(tmp_path, {"file.txt": "same content"})

        src2 = tmp_path / "src2"
        src2.mkdir()
        (src2 / "file.txt").write_text("same content")

        stage2 = stage_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)
        obj.update(obj_fs, stage2, created="2024-06-01T00:00:00Z")

        inv = obj.inventory.inventory
        v2_paths = [
            p for ps in inv.manifest.values() for p in ps if p.startswith("v2/")
        ]
        assert len(v2_paths) == 0

    def test_two_step_migration(self, tmp_path: Path):
        """Algorithm migration works with two-step API."""
        obj, obj_fs = _create_v1(tmp_path, {"file.txt": "hello"})

        src2 = tmp_path / "src2"
        src2.mkdir()
        (src2 / "file.txt").write_text("hello")
        (src2 / "new.txt").write_text("world")

        stage2 = stage_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)
        plan = obj.new_update_plan(stage2, created="2024-06-01T00:00:00Z")
        obj.apply_update_plan(obj_fs, plan, stage2.content_source)

        assert obj.inventory.inventory.digest_algorithm == "sha512"

        result = ValidationResult()
        validate_object(obj_fs, "", result)
        assert result.is_valid, f"errors: {[e.message for e in result.errors]}"

    def test_existing_fixity_preserved(self, tmp_path: Path):
        """Pre-existing fixity (e.g. md5) preserved through migration."""
        src = _make_src_dir(tmp_path, {"file.txt": "hello"})
        obj_dir = tmp_path / "obj"
        obj_dir.mkdir()

        src_fs = LocalFS(src.parent)
        stage1 = stage_dir(
            src_fs, "src", DigestAlgorithm.SHA256, fixity_algs=[DigestAlgorithm.MD5]
        )

        obj_fs = LocalFS(obj_dir)
        obj = Object(obj_fs, "")
        obj.update(
            obj_fs,
            stage1,
            id="test:migration",
            spec=Spec.V1_1,
            created="2024-01-01T00:00:00Z",
        )

        assert obj.inventory.inventory.fixity is not None
        assert "md5" in obj.inventory.inventory.fixity

        # Migrate to sha512
        src2 = tmp_path / "src2"
        src2.mkdir()
        (src2 / "file.txt").write_text("hello")

        stage2 = stage_dir(LocalFS(tmp_path), "src2", DigestAlgorithm.SHA512)
        obj.update(obj_fs, stage2, created="2024-06-01T00:00:00Z")

        inv = obj.inventory.inventory
        assert inv.fixity is not None
        assert "md5" in inv.fixity  # preserved
        assert "sha256" in inv.fixity  # added from migration

        result = ValidationResult()
        validate_object(obj_fs, "", result)
        assert result.is_valid, f"errors: {[e.message for e in result.errors]}"

    def test_migration_error_reverts(self, tmp_path: Path):
        """update() auto-reverts on error during migration write."""
        import pytest

        from ocfl_lib.digest import bytes_digest
        from ocfl_lib.stage import Stage

        obj, obj_fs = _create_v1(tmp_path, {"file.txt": "hello"})

        new_digest = bytes_digest(b"brand_new", DigestAlgorithm.SHA512)

        class FailingSource:
            def get_content(self, digest: str):
                raise OSError("disk full")

        sha512_digest = bytes_digest(b"hello", DigestAlgorithm.SHA512)
        stage = Stage(
            state={
                sha512_digest: ["file.txt"],
                new_digest: ["new.txt"],
            },
            digest_algorithm=DigestAlgorithm.SHA512,
            content_source=FailingSource(),
        )

        with pytest.raises(OSError, match="disk full"):
            obj.update(obj_fs, stage, created="2024-06-01T00:00:00Z")

        # v2 directory should be cleaned up by revert
        obj_dir = tmp_path / "obj"
        assert not (obj_dir / "v2").exists()

        # Root inventory must be restored to original SHA256 bytes
        original_inv_bytes = (obj_dir / "v1" / "inventory.json").read_bytes()
        assert (obj_dir / "inventory.json").read_bytes() == original_inv_bytes

        # Original SHA256 sidecar must be restored
        assert (obj_dir / "inventory.json.sha256").is_file()
        # New SHA512 sidecar must not exist
        assert not (obj_dir / "inventory.json.sha512").exists()

        # Object should still validate
        result = ValidationResult()
        validate_object(obj._fsys, "", result)
        assert result.is_valid, result.errors

    def test_migration_revert_restores_original_inventory(self, tmp_path: Path):
        """Revert after late-step failure restores original-algorithm inventory."""
        from ocfl_lib.update_plan import execute_plan, prepare_steps, revert_plan

        obj, obj_fs = _create_v1(tmp_path, {"file.txt": "hello"})

        # Save original on-disk inventory bytes (SHA256)
        obj_dir = tmp_path / "obj"
        orig_inv_bytes = (obj_dir / "inventory.json").read_bytes()

        # Build migration stage (SHA256 → SHA512)
        src2 = _make_src_dir(tmp_path, {"file.txt": "hello"})
        stage = stage_dir(LocalFS(src2.parent), src2.name, DigestAlgorithm.SHA512)

        # Use two-step API to inject failure at root-sidecar step
        plan = obj.new_update_plan(stage, created="2024-06-01T00:00:00Z")
        prepare_steps(plan, obj_fs, "", stage.content_source)

        # Find and replace root-sidecar run with failure
        def _fail() -> int:
            raise RuntimeError("injected")

        for step in plan.steps:
            if step.name == "root-sidecar":
                step.run = _fail

        import pytest

        with pytest.raises(RuntimeError, match="injected"):
            execute_plan(plan)

        revert_plan(plan)

        # Root inventory must be original SHA256 bytes
        assert (obj_dir / "inventory.json").read_bytes() == orig_inv_bytes
        # Original SHA256 sidecar must be restored
        assert (obj_dir / "inventory.json.sha256").is_file()
        # New SHA512 sidecar must not exist
        assert not (obj_dir / "inventory.json.sha512").exists()
