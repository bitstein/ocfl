import json
from io import BytesIO
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app
from ocfl_lib import (
    CommitState,
    DigestAlgorithm,
    LocalFS,
    Spec,
    StorageRoot,
    get_layout,
    scan_dir,
)
from ocfl_lib.validation import validate_root
from tests.helpers.faults import Crash

OID = "obj:rec"


def _root(tmp_path: Path) -> tuple[Path, LocalFS, StorageRoot]:
    d = tmp_path / "root"
    d.mkdir()
    fs = LocalFS(d)
    layout = get_layout("0002-flat-direct-storage-layout")
    StorageRoot.init(fs, fs, spec=Spec.V1_1, layout=layout)
    root = StorageRoot.open(fs)
    return d, fs, root


def _stage(tmp_path: Path, name: str, files: dict[str, bytes]):
    src = tmp_path / name
    for p, b in files.items():
        (src / p).parent.mkdir(parents=True, exist_ok=True)
        (src / p).write_bytes(b)
    return scan_dir(LocalFS(tmp_path), name, DigestAlgorithm.SHA512)


def _v2_claimed_by_dead_writer(tmp_path: Path) -> tuple[Path, LocalFS, StorageRoot]:
    d, fs, root = _root(tmp_path)
    root.commit(fs, OID, _stage(tmp_path, "s1", {"a.txt": b"a"}))

    class DiesAtRootInventory(LocalFS):
        def write(self, name, data):
            if name == f"{OID}/inventory.json":
                raise Crash
            return super().write(name, data)

    with pytest.raises(Crash):
        root.commit(DiesAtRootInventory(d), OID, _stage(tmp_path, "s2", {"b": b"b"}))
    assert root.commit_status(OID).state is CommitState.CLAIMED
    return d, fs, root


def _stray(tmp_path: Path) -> tuple[Path, LocalFS, StorageRoot]:
    d, fs, root = _root(tmp_path)
    root.commit(fs, OID, _stage(tmp_path, "s1", {"a.txt": b"a"}))
    root.commit(fs, OID, _stage(tmp_path, "s2", {"b": b"b"}))
    for f in ("inventory.json", "inventory.json.sha512"):
        fs.write(f"{OID}/{f}", BytesIO((d / OID / "v1" / f).read_bytes()))
    assert root.commit_status(OID).state is CommitState.STRAY
    return d, fs, root


def _run(d: Path, *args: str):
    return CliRunner().invoke(app, ["--root", str(d), *args])


def _head(root: StorageRoot) -> str:
    return str(root.get_object(OID).head)


def _clean(fs: LocalFS, root: StorageRoot) -> None:
    assert validate_root(fs, "").is_valid
    assert root.commit_status(OID).state is CommitState.CLEAN


class TestRecover:
    def test_help_lists_actions(self, tmp_path):
        r = CliRunner().invoke(app, ["recover", "--help"])
        assert r.exit_code == ExitCode.SUCCESS
        for flag in ("--resume", "--revert", "--remove-stray"):
            assert flag in r.output

    @pytest.mark.parametrize(
        "args", [(), ("--resume", "--revert"), ("--revert", "--remove-stray", "v2")]
    )
    def test_exactly_one_action(self, tmp_path, args):
        d, _fs, _r = _root(tmp_path)
        assert _run(d, "recover", OID, *args).exit_code == ExitCode.USAGE

    def test_revert(self, tmp_path):
        d, fs, root = _v2_claimed_by_dead_writer(tmp_path)
        r = _run(d, "recover", OID, "--revert")
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert "claimed v2" in r.output
        assert _head(root) == "v1"
        _clean(fs, root)

    def test_resume(self, tmp_path):
        d, fs, root = _v2_claimed_by_dead_writer(tmp_path)
        r = _run(d, "recover", OID, "--resume")
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert _head(root) == "v2"
        _clean(fs, root)

    def test_remove_stray(self, tmp_path):
        d, fs, root = _stray(tmp_path)
        r = _run(d, "recover", OID, "--remove-stray", "v1")
        assert r.exit_code != ExitCode.SUCCESS
        assert (d / OID / "v2").is_dir()
        r = _run(d, "recover", OID, "--remove-stray", "v2")
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert _head(root) == "v1"
        _clean(fs, root)

    def test_stray_cannot_be_resumed(self, tmp_path):
        d, _fs, _r = _stray(tmp_path)
        r = _run(d, "recover", OID, "--resume")
        assert r.exit_code != ExitCode.SUCCESS
        assert "remove-stray" in r.output

    def test_clean_object(self, tmp_path):
        d, fs, root = _root(tmp_path)
        root.commit(fs, OID, _stage(tmp_path, "s1", {"a.txt": b"a"}))
        r = _run(d, "recover", OID, "--revert")
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert "no unfinished commit" in r.output

    def test_json(self, tmp_path):
        d, _fs, _r = _v2_claimed_by_dead_writer(tmp_path)
        r = _run(d, "--json", "recover", OID, "--revert")
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert json.loads(r.stdout) == {
            "object_id": OID,
            "state": "claimed",
            "action": "revert",
            "version": "v2",
        }

    def test_commit_blocked_by_unfinished_commit_names_recover(self, tmp_path):
        d, fs, root = _v2_claimed_by_dead_writer(tmp_path)
        with pytest.raises(Exception, match="recover"):
            root.commit(fs, OID, _stage(tmp_path, "s3", {"c": b"c"}))
        assert _run(d, "recover", OID, "--revert").exit_code == ExitCode.SUCCESS
        root.commit(fs, OID, _stage(tmp_path, "s4", {"c": b"c"}))
        assert _head(root) == "v2"
