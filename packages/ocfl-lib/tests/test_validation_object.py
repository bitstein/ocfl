import hashlib
import json
import os
import re
import shutil
from pathlib import Path

import boto3
import pytest
from builders import FIXTURES_ROOT, inventory
from digests import D1
from moto import mock_aws

from ocfl_lib.fs._local import LocalFS
from ocfl_lib.fs._protocols import FsEntry
from ocfl_lib.fs.s3 import S3FS
from ocfl_lib.validation._object import split_declarations, validate_object
from ocfl_lib.validation._report import ValidationReport

_CODE_RE = re.compile(r"[EW]\d+")


def _parse_codes(dirname: str) -> list[str]:
    return _CODE_RE.findall(dirname)


def _has_fixture_error_code(result: ValidationReport, expected: str) -> bool:
    for error in result.errors:
        match = _CODE_RE.match(error.code)
        if match is not None and match.group(0) == expected:
            return True
    return False


def _collect_fixtures(spec_ver: str, category: str) -> list[tuple[str, Path]]:
    d = FIXTURES_ROOT / spec_ver / category
    if not d.exists():
        return []
    return [(p.name, p) for p in sorted(p for p in d.iterdir() if p.is_dir())]


@pytest.mark.parametrize(
    "name,path",
    _collect_fixtures("1.1", "good-objects") + _collect_fixtures("1.0", "good-objects"),
    ids=lambda x: x if isinstance(x, str) else "",
)
class TestGoodObjects:
    def test_valid(self, name: str, path: Path):
        fsys = LocalFS(path)
        r = validate_object(fsys, "")
        assert r.is_valid, f"{name}: {[(e.code, e.render()) for e in r.errors]}"


@pytest.mark.parametrize(
    "name,path",
    _collect_fixtures("1.1", "bad-objects") + _collect_fixtures("1.0", "bad-objects"),
    ids=lambda x: x if isinstance(x, str) else "",
)
class TestBadObjects:
    def test_invalid(self, name: str, path: Path):
        fsys = LocalFS(path)
        r = validate_object(fsys, "")
        assert not r.is_valid, f"{name}: expected invalid"

    def test_expected_codes(self, name: str, path: Path):
        codes = _parse_codes(name)
        if not codes:
            pytest.skip("no code in dir name")
        fsys = LocalFS(path)
        r = validate_object(fsys, "")
        actual = [e.code for e in r.errors]
        for code in codes:
            assert _has_fixture_error_code(r, code), (
                f"{name}: expected {code}, got {actual}"
            )


@pytest.mark.parametrize(
    "name,path",
    _collect_fixtures("1.1", "warn-objects") + _collect_fixtures("1.0", "warn-objects"),
    ids=lambda x: x if isinstance(x, str) else "",
)
class TestWarnObjects:
    def test_valid_with_warnings(self, name: str, path: Path):
        fsys = LocalFS(path)
        r = validate_object(fsys, "")
        assert r.is_valid, f"{name}: {[(e.code, e.render()) for e in r.errors]}"
        assert r.num_warnings > 0, f"{name}: expected warnings"

    def test_expected_warning_codes(self, name: str, path: Path):
        codes = _parse_codes(name)
        if not codes:
            pytest.skip("no code in dir name")
        fsys = LocalFS(path)
        r = validate_object(fsys, "")
        for code in codes:
            assert r.has_code(code), (
                f"{name}: expected {code}, got {[w.code for w in r.warnings]}"
            )


def test_spec_tuple_orders_numerically():
    from ocfl_lib.validation._object import _spec_tuple

    assert _spec_tuple("1.2") < _spec_tuple("1.10")
    assert _spec_tuple("1.0") < _spec_tuple("1.1")
    assert _spec_tuple("2.0") > _spec_tuple("1.10")


def _make_object(
    tmp_path: Path,
    root_inv: dict,
    *,
    version_inv: dict | None = None,
    empty_content: bool = False,
) -> None:
    (tmp_path / "0=ocfl_object_1.1").write_text("ocfl_object_1.1\n")
    inv_bytes = json.dumps(root_inv, sort_keys=True, separators=(",", ":")).encode()
    (tmp_path / "inventory.json").write_bytes(inv_bytes)
    digest = hashlib.sha512(inv_bytes).hexdigest()
    (tmp_path / "inventory.json.sha512").write_text(f"{digest} inventory.json\n")

    v1 = tmp_path / "v1"
    v1.mkdir()

    vinv = version_inv if version_inv is not None else root_inv
    vinv_bytes = json.dumps(vinv, sort_keys=True, separators=(",", ":")).encode()
    (v1 / "inventory.json").write_bytes(vinv_bytes)
    vdigest = hashlib.sha512(vinv_bytes).hexdigest()
    (v1 / "inventory.json.sha512").write_text(f"{vdigest} inventory.json\n")

    content_dir = root_inv.get("contentDirectory", "content")
    content_path = v1 / content_dir
    if empty_content:
        content_path.mkdir(exist_ok=True)
    elif not content_path.exists():
        content_path.mkdir(exist_ok=True)
        manifest = root_inv.get("manifest", {})
        for paths in manifest.values():
            if isinstance(paths, list):
                for p in paths:
                    fp = tmp_path / p
                    fp.parent.mkdir(parents=True, exist_ok=True)
                    fp.write_text("data")


class TestE019ContentDirectoryAdded:
    def test_version_sets_cd_when_root_absent(self, tmp_path: Path):
        root_inv = inventory()
        ver_inv = inventory(contentDirectory="content")
        _make_object(tmp_path, root_inv, version_inv=ver_inv)
        fsys = LocalFS(tmp_path)
        r = validate_object(fsys, "", check_digests=False)
        assert r.has_code("E019")

    def test_root_cd_differs_from_first_version_cd(self, tmp_path: Path):
        root_inv = inventory(contentDirectory="content")
        ver_inv = inventory(contentDirectory="data")
        _make_object(tmp_path, root_inv, version_inv=ver_inv)
        fsys = LocalFS(tmp_path)
        r = validate_object(fsys, "", check_digests=False)
        assert r.has_code("E019")


class TestE020ContentDirectoryMismatch:
    def test_version_cd_differs_from_root(self, tmp_path: Path):
        root_inv = inventory(contentDirectory="content")
        ver_inv = inventory(contentDirectory="data")
        _make_object(tmp_path, root_inv, version_inv=ver_inv)
        fsys = LocalFS(tmp_path)
        r = validate_object(fsys, "", check_digests=False)
        assert r.has_code("E020")


class TestE010VersionDirSequence:
    def test_version_dirs_must_be_contiguous(self, tmp_path: Path):
        root_inv = inventory(
            head="v4",
            versions={
                "v1": {
                    "created": "2024-01-01T00:00:00Z",
                    "state": {D1: ["file.txt"]},
                },
                "v2": {
                    "created": "2024-01-02T00:00:00Z",
                    "state": {D1: ["file.txt"]},
                },
                "v3": {
                    "created": "2024-01-03T00:00:00Z",
                    "state": {D1: ["file.txt"]},
                },
                "v4": {
                    "created": "2024-01-04T00:00:00Z",
                    "state": {D1: ["file.txt"]},
                },
            },
        )
        _make_object(tmp_path, root_inv)
        (tmp_path / "v4").mkdir()

        fsys = LocalFS(tmp_path)
        r = validate_object(fsys, "", check_digests=False)

        assert r.has_code("E010")


class TestVersionInventoryJson:
    def test_non_head_inventory_manifest_digest_mismatch_fires_e092(
        self, tmp_path: Path
    ):
        content = b"data"
        digest = hashlib.sha512(content).hexdigest()
        bad_digest = "0" + digest[1:]
        root_inv = {
            "id": "http://example.org/obj1",
            "type": "https://ocfl.io/1.1/spec/#inventory",
            "digestAlgorithm": "sha512",
            "head": "v2",
            "manifest": {digest: ["v1/content/file.txt", "v2/content/file.txt"]},
            "versions": {
                "v1": {
                    "created": "2024-01-01T00:00:00Z",
                    "state": {digest: ["file.txt"]},
                },
                "v2": {
                    "created": "2024-01-02T00:00:00Z",
                    "state": {digest: ["file.txt"]},
                },
            },
        }
        v1_inv = {
            **root_inv,
            "head": "v1",
            "manifest": {bad_digest: ["v1/content/file.txt"]},
            "versions": {
                "v1": {
                    "created": "2024-01-01T00:00:00Z",
                    "state": {bad_digest: ["file.txt"]},
                },
            },
        }

        (tmp_path / "0=ocfl_object_1.1").write_text("ocfl_object_1.1\n")
        root_inv_bytes = json.dumps(
            root_inv, sort_keys=True, separators=(",", ":")
        ).encode()
        root_inv_digest = hashlib.sha512(root_inv_bytes).hexdigest()
        (tmp_path / "inventory.json").write_bytes(root_inv_bytes)
        (tmp_path / "inventory.json.sha512").write_text(
            f"{root_inv_digest} inventory.json\n"
        )

        v1 = tmp_path / "v1"
        (v1 / "content").mkdir(parents=True)
        (v1 / "content" / "file.txt").write_bytes(content)
        v1_inv_bytes = json.dumps(
            v1_inv, sort_keys=True, separators=(",", ":")
        ).encode()
        (v1 / "inventory.json").write_bytes(v1_inv_bytes)
        (v1 / "inventory.json.sha512").write_text(
            f"{hashlib.sha512(v1_inv_bytes).hexdigest()} inventory.json\n"
        )

        v2 = tmp_path / "v2"
        (v2 / "content").mkdir(parents=True)
        (v2 / "content" / "file.txt").write_bytes(content)
        (v2 / "inventory.json").write_bytes(root_inv_bytes)
        (v2 / "inventory.json.sha512").write_text(f"{root_inv_digest} inventory.json\n")

        fsys = LocalFS(tmp_path)
        r = validate_object(fsys, "")

        assert r.has_code("E092")

    def test_non_head_alg_mismatch_fires_e092_even_in_lax_mode(self, tmp_path: Path):
        content = b"data"
        digest = hashlib.sha512(content).hexdigest()
        v1_alg_digest = hashlib.sha256(content).hexdigest()
        bad_v1_digest = "0" + v1_alg_digest[1:]
        root_inv = {
            "id": "http://example.org/obj1",
            "type": "https://ocfl.io/1.1/spec/#inventory",
            "digestAlgorithm": "sha512",
            "head": "v2",
            "manifest": {digest: ["v1/content/file.txt", "v2/content/file.txt"]},
            "versions": {
                "v1": {
                    "created": "2024-01-01T00:00:00Z",
                    "state": {digest: ["file.txt"]},
                },
                "v2": {
                    "created": "2024-01-02T00:00:00Z",
                    "state": {digest: ["file.txt"]},
                },
            },
        }
        v1_inv = {
            "id": "http://example.org/obj1",
            "type": "https://ocfl.io/1.1/spec/#inventory",
            "digestAlgorithm": "sha256",
            "head": "v1",
            "manifest": {bad_v1_digest: ["v1/content/file.txt"]},
            "versions": {
                "v1": {
                    "created": "2024-01-01T00:00:00Z",
                    "state": {bad_v1_digest: ["file.txt"]},
                },
            },
        }

        (tmp_path / "0=ocfl_object_1.1").write_text("ocfl_object_1.1\n")
        root_inv_bytes = json.dumps(
            root_inv, sort_keys=True, separators=(",", ":")
        ).encode()
        root_inv_digest = hashlib.sha512(root_inv_bytes).hexdigest()
        (tmp_path / "inventory.json").write_bytes(root_inv_bytes)
        (tmp_path / "inventory.json.sha512").write_text(
            f"{root_inv_digest} inventory.json\n"
        )

        v1 = tmp_path / "v1"
        (v1 / "content").mkdir(parents=True)
        (v1 / "content" / "file.txt").write_bytes(content)
        v1_inv_bytes = json.dumps(
            v1_inv, sort_keys=True, separators=(",", ":")
        ).encode()
        (v1 / "inventory.json").write_bytes(v1_inv_bytes)
        (v1 / "inventory.json.sha256").write_text(
            f"{hashlib.sha256(v1_inv_bytes).hexdigest()} inventory.json\n"
        )

        v2 = tmp_path / "v2"
        (v2 / "content").mkdir(parents=True)
        (v2 / "content" / "file.txt").write_bytes(content)
        (v2 / "inventory.json").write_bytes(root_inv_bytes)
        (v2 / "inventory.json.sha512").write_text(f"{root_inv_digest} inventory.json\n")

        fsys = LocalFS(tmp_path)
        r = validate_object(fsys, "", check_digests=False)

        assert r.has_code("E092")

    def test_missing_fixity_content_path_fires_e093(self, tmp_path: Path):
        content = b"data"
        digest = hashlib.sha512(content).hexdigest()
        fixity_digest = hashlib.md5(content, usedforsecurity=False).hexdigest()
        inv = {
            "id": "http://example.org/obj1",
            "type": "https://ocfl.io/1.1/spec/#inventory",
            "digestAlgorithm": "sha512",
            "head": "v1",
            "manifest": {digest: ["v1/content/file.txt", "v1/content/missing.txt"]},
            "versions": {
                "v1": {
                    "created": "2024-01-01T00:00:00Z",
                    "state": {digest: ["file.txt"]},
                },
            },
            "fixity": {
                "md5": {
                    fixity_digest: ["v1/content/file.txt", "v1/content/missing.txt"]
                }
            },
        }
        inv_bytes = json.dumps(inv, sort_keys=True, separators=(",", ":")).encode()
        inv_digest = hashlib.sha512(inv_bytes).hexdigest()

        (tmp_path / "0=ocfl_object_1.1").write_text("ocfl_object_1.1\n")
        (tmp_path / "inventory.json").write_bytes(inv_bytes)
        (tmp_path / "inventory.json.sha512").write_text(
            f"{inv_digest} inventory.json\n"
        )

        v1 = tmp_path / "v1"
        (v1 / "content").mkdir(parents=True)
        (v1 / "content" / "file.txt").write_bytes(content)
        (v1 / "inventory.json").write_bytes(inv_bytes)
        (v1 / "inventory.json.sha512").write_text(f"{inv_digest} inventory.json\n")

        fsys = LocalFS(tmp_path)
        r = validate_object(fsys, "")

        assert r.has_code("E093")

    def test_corrupt_non_head_inventory_fires_e033(self, tmp_path: Path):
        content = b"data"
        digest = hashlib.sha512(content).hexdigest()
        root_inv = {
            "id": "http://example.org/obj1",
            "type": "https://ocfl.io/1.1/spec/#inventory",
            "digestAlgorithm": "sha512",
            "head": "v2",
            "manifest": {digest: ["v1/content/file.txt"]},
            "versions": {
                "v1": {
                    "created": "2024-01-01T00:00:00Z",
                    "state": {digest: ["file.txt"]},
                },
                "v2": {
                    "created": "2024-01-02T00:00:00Z",
                    "state": {digest: ["file.txt"]},
                },
            },
        }
        root_inv_bytes = json.dumps(
            root_inv, sort_keys=True, separators=(",", ":")
        ).encode()
        root_inv_digest = hashlib.sha512(root_inv_bytes).hexdigest()

        (tmp_path / "0=ocfl_object_1.1").write_text("ocfl_object_1.1\n")
        (tmp_path / "inventory.json").write_bytes(root_inv_bytes)
        (tmp_path / "inventory.json.sha512").write_text(
            f"{root_inv_digest} inventory.json\n"
        )

        v1 = tmp_path / "v1"
        (v1 / "content").mkdir(parents=True)
        (v1 / "content" / "file.txt").write_bytes(content)
        corrupt_v1 = b'{"id":'
        (v1 / "inventory.json").write_bytes(corrupt_v1)
        (v1 / "inventory.json.sha512").write_text(
            f"{hashlib.sha512(corrupt_v1).hexdigest()} inventory.json\n"
        )

        v2 = tmp_path / "v2"
        (v2 / "content").mkdir(parents=True)
        (v2 / "inventory.json").write_bytes(root_inv_bytes)
        (v2 / "inventory.json.sha512").write_text(f"{root_inv_digest} inventory.json\n")

        fsys = LocalFS(tmp_path)
        r = validate_object(fsys, "")

        assert r.has_code("E033")

    def test_non_object_non_head_inventory_fires_e034(self, tmp_path: Path):
        content = b"data"
        digest = hashlib.sha512(content).hexdigest()
        root_inv = {
            "id": "http://example.org/obj1",
            "type": "https://ocfl.io/1.1/spec/#inventory",
            "digestAlgorithm": "sha512",
            "head": "v2",
            "manifest": {digest: ["v1/content/file.txt"]},
            "versions": {
                "v1": {
                    "created": "2024-01-01T00:00:00Z",
                    "state": {digest: ["file.txt"]},
                },
                "v2": {
                    "created": "2024-01-02T00:00:00Z",
                    "state": {digest: ["file.txt"]},
                },
            },
        }
        root_inv_bytes = json.dumps(
            root_inv, sort_keys=True, separators=(",", ":")
        ).encode()
        root_inv_digest = hashlib.sha512(root_inv_bytes).hexdigest()

        (tmp_path / "0=ocfl_object_1.1").write_text("ocfl_object_1.1\n")
        (tmp_path / "inventory.json").write_bytes(root_inv_bytes)
        (tmp_path / "inventory.json.sha512").write_text(
            f"{root_inv_digest} inventory.json\n"
        )

        v1 = tmp_path / "v1"
        (v1 / "content").mkdir(parents=True)
        (v1 / "content" / "file.txt").write_bytes(content)
        non_object_v1 = b"[]"
        (v1 / "inventory.json").write_bytes(non_object_v1)
        (v1 / "inventory.json.sha512").write_text(
            f"{hashlib.sha512(non_object_v1).hexdigest()} inventory.json\n"
        )

        v2 = tmp_path / "v2"
        (v2 / "content").mkdir(parents=True)
        (v2 / "inventory.json").write_bytes(root_inv_bytes)
        (v2 / "inventory.json.sha512").write_text(f"{root_inv_digest} inventory.json\n")

        fsys = LocalFS(tmp_path)
        r = validate_object(fsys, "")

        assert r.has_code("E034")


class TestE003cUnparseableDecl:
    def test_unparseable_zero_eq_fires_E003c(self, tmp_path: Path):
        (tmp_path / "0=garbage_1.1").write_text("garbage\n")
        (tmp_path / "v1").mkdir()
        fsys = LocalFS(tmp_path)
        r = validate_object(fsys, "")
        assert r.has_code("E003c")
        assert not any(e.code == "E003a" for e in r.errors)

    def test_no_zero_eq_at_all_fires_E003a(self, tmp_path: Path):
        (tmp_path / "v1").mkdir()
        (tmp_path / "readme.txt").write_text("hello\n")
        fsys = LocalFS(tmp_path)
        r = validate_object(fsys, "")
        assert any(e.code == "E003a" for e in r.errors)
        assert not r.has_code("E003c")


class TestE006InvalidNamaste:
    def test_unparseable_zero_eq_fires_E006(self, tmp_path: Path):
        (tmp_path / "0=garbage_1.1").write_text("garbage\n")
        (tmp_path / "0=alsogarbage").write_text("nope\n")
        (tmp_path / "v1").mkdir()
        fsys = LocalFS(tmp_path)
        r = validate_object(fsys, "")
        e006_errors = [e for e in r.errors if e.code == "E006"]
        msgs = [e.render() for e in e006_errors]
        assert any("0=garbage_1.1" in m for m in msgs)
        assert any("0=alsogarbage" in m for m in msgs)

    def test_valid_plus_unparseable_fires_E006_for_extra(self, tmp_path: Path):
        root_inv = inventory()
        _make_object(tmp_path, root_inv)
        (tmp_path / "0=garbage").write_text("nope\n")
        fsys = LocalFS(tmp_path)
        r = validate_object(fsys, "", check_digests=False)
        e006_errors = [e for e in r.errors if e.code == "E006"]
        assert len(e006_errors) == 1
        assert "0=garbage" in e006_errors[0].render()


class TestW003EmptyContentDir:
    def test_empty_content_dir_warns(self, tmp_path: Path):
        root_inv = inventory(
            manifest={},
            versions={
                "v1": {
                    "created": "2024-01-01T00:00:00Z",
                    "state": {},
                    "message": "init",
                    "user": {"name": "test", "address": "mailto:t@e.org"},
                },
            },
        )
        _make_object(tmp_path, root_inv, empty_content=True)
        fsys = LocalFS(tmp_path)
        r = validate_object(fsys, "", check_digests=False)
        assert r.has_code("W003")


class TestE024EmptyDirInContent:
    @pytest.mark.parametrize(
        "fixture,content_dir,empty_dir",
        [
            ("minimal_one_version_one_file", "content", "empty"),
            ("minimal_one_version_one_file", "content", "outer/inner"),
            ("minimal_content_dir_called_stuff", "stuff", "empty"),
        ],
    )
    def test_empty_dir_in_version_content_fires_E024(
        self, tmp_path: Path, fixture: str, content_dir: str, empty_dir: str
    ):
        obj = tmp_path / "obj"
        shutil.copytree(FIXTURES_ROOT / "1.1" / "good-objects" / fixture, obj)
        (obj / "v1" / content_dir / empty_dir).mkdir(parents=True)

        r = validate_object(LocalFS(obj), "")

        assert not r.is_valid
        assert [e.render() for e in r.errors] == [
            f"OCFL Object version v1 content directory includes empty path {empty_dir}"
        ]

    def test_s3_folder_marker_in_version_content_fires_E024(self):
        fixture = (
            FIXTURES_ROOT / "1.1" / "good-objects" / "minimal_one_version_one_file"
        )
        with mock_aws():
            client = boto3.client("s3", region_name="us-east-1")
            client.create_bucket(Bucket="test-bucket")
            for f in fixture.rglob("*"):
                if f.is_file():
                    key = f"obj/{f.relative_to(fixture).as_posix()}"
                    client.put_object(
                        Bucket="test-bucket", Key=key, Body=f.read_bytes()
                    )
            client.put_object(
                Bucket="test-bucket", Key="obj/v1/content/empty/", Body=b""
            )

            r = validate_object(S3FS("test-bucket", client=client), "obj")

        assert [e.render() for e in r.errors] == [
            "OCFL Object version v1 content directory includes empty path empty"
        ]


def _empty_state_inventory(*version_ids: str) -> dict:
    block = {
        "created": "2024-01-01T00:00:00Z",
        "state": {},
        "message": "init",
        "user": {"name": "test", "address": "mailto:t@e.org"},
    }
    return inventory(
        manifest={},
        head=version_ids[-1],
        versions={v: block for v in version_ids},
    )


def _codes(r: ValidationReport) -> list[str]:
    return sorted(f.code for f in r.findings)


def _validate_on_s3(obj: Path) -> ValidationReport:
    """Mirror obj to moto S3, each empty directory as a folder-marker key."""
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-bucket")
        for p in obj.rglob("*"):
            key = f"obj/{p.relative_to(obj).as_posix()}"
            if p.is_file():
                client.put_object(Bucket="test-bucket", Key=key, Body=p.read_bytes())
            elif not any(p.iterdir()):
                client.put_object(Bucket="test-bucket", Key=f"{key}/", Body=b"")
        return validate_object(S3FS("test-bucket", client=client), "obj")


class TestS3FolderMarkerParity:
    def test_empty_content_dir_is_W003(self, tmp_path: Path):
        _make_object(tmp_path, _empty_state_inventory("v1"), empty_content=True)

        r = _validate_on_s3(tmp_path)

        assert r.has_code("W003")
        assert _codes(r) == _codes(validate_object(LocalFS(tmp_path), ""))

    def test_empty_declared_version_dir_is_W010(self, tmp_path: Path):
        _make_object(tmp_path, _empty_state_inventory("v1", "v2"), empty_content=True)
        (tmp_path / "v2").mkdir()

        r = _validate_on_s3(tmp_path)

        assert r.has_code("W010")
        assert not r.has_code("E046")
        assert _codes(r) == _codes(validate_object(LocalFS(tmp_path), ""))

    def test_nested_empty_dir_in_content_is_E024(self, tmp_path: Path):
        shutil.copytree(
            FIXTURES_ROOT / "1.1" / "good-objects" / "minimal_one_version_one_file",
            tmp_path / "o",
        )
        (tmp_path / "o" / "v1" / "content" / "sub").mkdir()

        r = _validate_on_s3(tmp_path / "o")

        assert _codes(r) == ["E024"]
        assert _codes(r) == _codes(validate_object(LocalFS(tmp_path / "o"), ""))


def _set_inventory_type(vdir: Path, spec: str) -> None:
    inv_path = vdir / "inventory.json"
    raw = re.sub(
        rb"https://ocfl\.io/\d+\.\d+/spec/#inventory",
        f"https://ocfl.io/{spec}/spec/#inventory".encode(),
        inv_path.read_bytes(),
    )
    inv_path.write_bytes(raw)
    digest = hashlib.sha512(raw).hexdigest()
    (vdir / "inventory.json.sha512").write_text(f"{digest} inventory.json\n")


class TestE103SpecNeverOlderThanPrevious:
    def test_upgraded_object_is_valid(
        self, bad_objects_1_1: Path, tmp_path: Path
    ) -> None:
        obj = tmp_path / "obj"
        shutil.copytree(bad_objects_1_1 / "E103_older_spec_v2", obj)
        _set_inventory_type(obj / "v1", "1.0")
        _set_inventory_type(obj / "v2", "1.1")

        r = validate_object(LocalFS(obj), "")
        assert not r.has_code("E103")
        assert r.is_valid, [e.code for e in r.errors]

    def test_older_than_previous_version_reports_against_it(
        self, bad_objects_1_1: Path
    ) -> None:
        r = validate_object(LocalFS(bad_objects_1_1 / "E103_older_spec_v2"), "")
        e103 = [e for e in r.errors if e.code == "E103"]
        assert len(e103) == 1
        assert "v2 inventory" in e103[0].render()
        assert "the v1 inventory" in e103[0].render()


class TestE038aRootTypeMatchesDeclaration:
    @pytest.mark.parametrize(("decl", "inv_spec"), [("1.1", "1.0"), ("1.0", "1.1")])
    def test_root_type_other_than_declaration(
        self, tmp_path: Path, decl: str, inv_spec: str
    ) -> None:
        obj = tmp_path / "obj"
        shutil.copytree(
            FIXTURES_ROOT / decl / "good-objects/minimal_one_version_one_file", obj
        )
        _set_inventory_type(obj, inv_spec)
        _set_inventory_type(obj / "v1", inv_spec)

        r = validate_object(LocalFS(obj), "")

        assert [e.render() for e in r.errors] == [
            "OCFL Object root inventory type attribute has wrong value"
            f" (expected https://ocfl.io/{decl}/spec/#inventory,"
            f" got https://ocfl.io/{inv_spec}/spec/#inventory)"
        ]

    def test_unparseable_type_is_not_e038a(self, tmp_path: Path) -> None:
        obj = tmp_path / "obj"
        shutil.copytree(
            FIXTURES_ROOT / "1.1/good-objects/minimal_one_version_one_file", obj
        )
        _set_inventory_type(obj, "x")
        _set_inventory_type(obj / "v1", "x")

        r = validate_object(LocalFS(obj), "")

        assert [e.code for e in r.errors] == ["E038b", "E038b"]


@pytest.mark.parametrize(
    "extension",
    [
        "0008-schema-registry",
        "0009-digest-algorithms",
        "0010-differential-n-tuple-omit-prefix-storage-layout",
        "0011-direct-clean-path-layout",
        "0012-hash-and-no-prefix-id-n-tuple-storage-layout",
    ],
)
def test_registered_extension_dir_is_not_w013(tmp_path: Path, extension: str):
    obj = tmp_path / "o"
    shutil.copytree(
        FIXTURES_ROOT / "1.1/good-objects/minimal_one_version_one_file", obj
    )
    (obj / "extensions" / extension).mkdir(parents=True)
    (obj / "extensions" / extension / "config.json").write_text("{}")

    result = validate_object(LocalFS(tmp_path), "o")

    assert [f.code for f in result.findings] == []


@pytest.mark.parametrize(
    "names",
    [
        ["0=ocfl_object_1.0", "0=ocfl_object_1.1"],
        ["0=ocfl_object_1.1", "0=ocfl_object_1.0"],
    ],
)
def test_highest_declaration_is_used_whatever_the_listing_order(names):
    entries = [FsEntry(name=n, is_dir=False, is_file=True, size=0) for n in names]

    _unparsed, decls = split_declarations(entries, "object")

    assert [str(d.spec) for d in decls] == ["1.1", "1.0"]


def _write_inventory(vdir: Path, inv: dict, sidecar_alg: str) -> bytes:
    inv_bytes = json.dumps(inv, sort_keys=True, separators=(",", ":")).encode()
    (vdir / "inventory.json").write_bytes(inv_bytes)
    digest = hashlib.new(sidecar_alg, inv_bytes).hexdigest()
    (vdir / f"inventory.json.{sidecar_alg}").write_text(f"{digest} inventory.json\n")
    return inv_bytes


def _two_version_object(tmp_path: Path, *, v1_alg: str, v1_sidecar_alg: str) -> None:
    """v1 inventory written under `v1_alg`, v2 (head) under sha512."""
    content = b"data"
    (tmp_path / "0=ocfl_object_1.1").write_text("ocfl_object_1.1\n")
    v1_digest = hashlib.new(v1_alg, content).hexdigest()
    meta = {"message": "m", "user": {"name": "n", "address": "mailto:n@example.org"}}
    v1_inv = {
        "id": "http://example.org/obj1",
        "type": "https://ocfl.io/1.1/spec/#inventory",
        "digestAlgorithm": v1_alg,
        "head": "v1",
        "manifest": {v1_digest: ["v1/content/file.txt"]},
        "versions": {
            "v1": {"created": "2024-01-01T00:00:00Z", "state": {v1_digest: ["f"]}}
            | meta,
        },
    }
    digest = hashlib.sha512(content).hexdigest()
    root_inv = {
        **v1_inv,
        "digestAlgorithm": "sha512",
        "head": "v2",
        "manifest": {digest: ["v1/content/file.txt"]},
        "versions": {
            "v1": {"created": "2024-01-01T00:00:00Z", "state": {digest: ["f"]}} | meta,
            "v2": {"created": "2024-01-02T00:00:00Z", "state": {digest: ["f"]}} | meta,
        },
    }
    (tmp_path / "v1" / "content").mkdir(parents=True)
    (tmp_path / "v2").mkdir()
    (tmp_path / "v1" / "content" / "file.txt").write_bytes(content)
    _write_inventory(tmp_path / "v1", v1_inv, v1_sidecar_alg)
    _write_inventory(tmp_path / "v2", root_inv, "sha512")
    _write_inventory(tmp_path, root_inv, "sha512")


class TestVersionSidecarAlgorithm:
    def test_sidecar_in_other_algorithm_fires_e058a(self, tmp_path: Path):
        obj = tmp_path / "o"
        shutil.copytree(
            FIXTURES_ROOT / "1.1/good-objects/minimal_one_version_one_file", obj
        )
        v1 = obj / "v1"
        (v1 / "inventory.json.sha512").unlink()
        md5 = hashlib.md5((v1 / "inventory.json").read_bytes(), usedforsecurity=False)
        (v1 / "inventory.json.md5").write_text(f"{md5.hexdigest()} inventory.json\n")

        r = validate_object(LocalFS(tmp_path), "o")

        assert sorted(
            ((f.code, dict(f.detail)) for f in r.findings), key=lambda t: t[0]
        ) == [
            ("E015", {"where": "v1", "entry": "inventory.json.md5"}),
            ("E058a", {"where": "v1", "path": "o/v1/inventory.json.sha512"}),
        ]

    def test_sidecar_follows_its_own_inventory_algorithm(self, tmp_path: Path):
        _two_version_object(tmp_path, v1_alg="sha256", v1_sidecar_alg="sha256")

        r = validate_object(LocalFS(tmp_path), "")

        assert [f.code for f in r.findings] == ["W004"]

    def test_sidecar_in_root_algorithm_for_older_inventory_fires_e058a(
        self, tmp_path: Path
    ):
        _two_version_object(tmp_path, v1_alg="sha256", v1_sidecar_alg="sha512")

        r = validate_object(LocalFS(tmp_path), "")

        assert sorted(
            ((f.code, dict(f.detail)) for f in r.findings), key=lambda t: t[0]
        ) == [
            ("E015", {"where": "v1", "entry": "inventory.json.sha512"}),
            ("E058a", {"where": "v1", "path": "v1/inventory.json.sha256"}),
            ("W004", {"where": "v1"}),
        ]


@pytest.mark.parametrize("sidecar", ["", " \n"])
def test_empty_root_sidecar_fires_e061(tmp_path: Path, sidecar: str):
    obj = tmp_path / "o"
    shutil.copytree(
        FIXTURES_ROOT / "1.1/good-objects/minimal_one_version_one_file", obj
    )
    (obj / "inventory.json.sha512").write_text(sidecar)

    r = validate_object(LocalFS(tmp_path), "o")

    assert [(f.code, dict(f.detail)) for f in r.findings] == [
        ("E061", {"description": "sidecar must contain digest and filename"})
    ]


def _findings(r: ValidationReport) -> list[tuple[str, dict[str, str]]]:
    return sorted(((f.code, dict(f.detail)) for f in r.findings), key=lambda t: t[0])


class TestUnsupportedFileTypes:
    @pytest.fixture
    def obj(self, tmp_path: Path) -> Path:
        obj = tmp_path / "o"
        shutil.copytree(
            FIXTURES_ROOT / "1.1/good-objects/minimal_one_version_one_file", obj
        )
        return obj

    @pytest.mark.parametrize(
        "link",
        ["link", "v1/link", "v1/content/link"],
    )
    def test_file_symlink_fires_E090(self, obj: Path, link: str):
        (obj / link).symlink_to(obj / "v1/content/a_file.txt")

        r = validate_object(LocalFS(obj.parent), "o")

        assert _findings(r) == [("E090", {"path": f"o/{link}"})]

    def test_dir_symlink_in_content_fires_E090(self, obj: Path):
        (obj / "v1/content/dirlink").symlink_to(obj / "v1", target_is_directory=True)

        r = validate_object(LocalFS(obj.parent), "o")

        assert _findings(r) == [("E090", {"path": "o/v1/content/dirlink"})]

    def test_fifo_in_content_fires_L003(self, obj: Path):
        os.mkfifo(obj / "v1/content/pipe")

        r = validate_object(LocalFS(obj.parent), "o")

        assert _findings(r) == [("L003", {"path": "o/v1/content/pipe", "kind": "fifo"})]

    def test_other_findings_survive(self, obj: Path):
        (obj / "link").symlink_to(obj / "inventory.json")
        (obj / "stray.txt").write_text("x")

        r = validate_object(LocalFS(obj.parent), "o")

        assert _findings(r) == [
            ("E001a", {"file": "stray.txt"}),
            ("E090", {"path": "o/link"}),
        ]
