import shutil
from pathlib import Path

from ocfl_lib import (
    DigestAlgorithm,
    LocalFS,
    Spec,
    StorageRoot,
    VNum,
    get_layout,
    scan_dir,
)

SEED_ID = "obj:test"
SEED_DIGEST = DigestAlgorithm.SHA512
SEED_HEAD = VNum(2)


def build_seed_root(root: Path) -> None:
    fs = LocalFS(root)
    layout = get_layout("0002-flat-direct-storage-layout")
    storage_root = StorageRoot.init(fs, fs, spec=Spec.V1_1, layout=layout)

    src = root / "test-files"
    src.mkdir()
    (src / "file1.txt").write_text("hello")
    storage_root.commit(
        fs, SEED_ID, scan_dir(fs, "test-files", SEED_DIGEST), message="v1"
    )
    (src / "file2.txt").write_text("world")
    storage_root.commit(
        fs, SEED_ID, scan_dir(fs, "test-files", SEED_DIGEST), message="v2"
    )
    shutil.rmtree(src)
