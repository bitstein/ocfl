import hashlib
import json
from pathlib import Path

import pytest

from ocfl_lib import LocalFS, OtsConfigError, ots
from ocfl_lib.fs import open_fs
from tests.helpers.ots import build_attestation

EXT = ots.EXTENSION_NAME


def _object(root: Path, *, config: dict | None, timestamp_from: str | None = None):
    """Two versions; v2 renames a.txt and adds b.txt, each with its proof."""
    a, b = b"alpha", b"beta"
    pa = build_attestation(hashlib.sha256(a).digest(), block_height=1)
    pb = build_attestation(hashlib.sha256(b).digest(), block_height=1)

    def d(x: bytes) -> str:
        return hashlib.sha256(x).hexdigest()

    manifest = {
        d(a): ["v1/content/a.txt"],
        d(pa): ["v1/content/a.txt.ots"],
        d(b): ["v2/content/b.txt"],
        d(pb): ["v2/content/b.txt.ots"],
    }
    v1 = {"state": {d(a): ["a.txt"], d(pa): ["a.txt.ots"]}}
    v2 = {
        "state": {
            d(a): ["c.txt"],
            d(pa): ["c.txt.ots"],
            d(b): ["b.txt"],
            d(pb): ["b.txt.ots"],
        }
    }
    inv = {"id": "x", "head": "v2", "manifest": manifest, "versions": {}}
    root.mkdir()
    for v, block in (("v1", v1), ("v2", v2)):
        inv["versions"][v] = block
        (root / v).mkdir()
        (root / v / "inventory.json").write_text(json.dumps(inv))
    (root / "inventory.json").write_text(json.dumps(inv))
    for cp, data in (
        ("v1/content/a.txt", a),
        ("v1/content/a.txt.ots", pa),
        ("v2/content/b.txt", b),
        ("v2/content/b.txt.ots", pb),
    ):
        (root / cp).parent.mkdir(parents=True, exist_ok=True)
        (root / cp).write_bytes(data)
    if config is not None:
        ext = root / ots.EXTENSION_DIR
        ext.mkdir(parents=True)
        (ext / "config.json").write_text(json.dumps(config))
        for v in ("v1", "v2"):
            raw = (root / v / "inventory.json").read_bytes()
            proof = build_attestation(hashlib.sha256(raw).digest(), block_height=1)
            (root / ots.inventory_proof_path(v)).write_bytes(proof)


def test_paths():
    assert f"extensions/{EXT}/config.json" == ots.CONFIG_PATH
    assert ots.inventory_proof_path("v02") == f"extensions/{EXT}/inventory.json.v02.ots"


def test_config_defaults_round_trip():
    cfg = ots.OtsConfig(timestamp_from="v2")
    assert ots.OtsConfig.from_json(cfg.to_json()) == cfg
    assert json.loads(ots.OtsConfig().to_json()) == {
        "extensionName": EXT,
        "proofDigestAlgorithm": "sha256",
        "timestampContent": True,
        "timestampInventory": True,
    }


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b'{"extensionName": "other"}',
        b'{"extensionName": "%s", "timestampFrom": 1}' % EXT.encode(),
    ],
)
def test_config_invalid(raw):
    with pytest.raises(OtsConfigError):
        ots.OtsConfig.from_json(raw)


def test_read_config_by_directory(tmp_path):
    fs = LocalFS(tmp_path)
    _object(tmp_path / "off", config=None)
    assert ots.read_config(fs, "off") is None
    _object(tmp_path / "on", config={"extensionName": EXT})
    (tmp_path / "on" / ots.CONFIG_PATH).unlink()
    with pytest.raises(OtsConfigError):
        ots.read_config(fs, "on")


def test_covered_versions():
    cfg = ots.OtsConfig(timestamp_from="v2")
    assert ots.covered_versions(cfg, ["v10", "v2", "v1"]) == ["v2", "v10"]
    assert ots.covered_versions(ots.OtsConfig(), ["v2", "v1"]) == ["v1", "v2"]
    with pytest.raises(OtsConfigError):
        ots.covered_versions(ots.OtsConfig(timestamp_from="v3"), ["v1"])


def test_object_proofs_content_once_per_file(tmp_path):
    _object(tmp_path / "o", config={"extensionName": EXT})
    refs = ots.object_proofs(LocalFS(tmp_path), "o")
    content = [(r.version, r.path) for r in refs if r.kind == "content"]
    assert content == [("v1", "v1/content/a.txt.ots"), ("v2", "v2/content/b.txt.ots")]
    inventory = [r.path for r in refs if r.kind == "inventory"]
    assert inventory == [ots.inventory_proof_path("v1"), ots.inventory_proof_path("v2")]
    raw = (tmp_path / "o/v2/inventory.json").read_bytes()
    assert refs[-1].digest == hashlib.sha256(raw).hexdigest()


def test_object_proofs_honor_flags(tmp_path):
    cfg = {"extensionName": EXT, "timestampContent": False, "timestampFrom": "v2"}
    _object(tmp_path / "o", config=cfg)
    refs = ots.object_proofs(LocalFS(tmp_path), "o")
    assert [(r.kind, r.version) for r in refs] == [("inventory", "v2")]


def test_open_fs_local(tmp_path):
    assert isinstance(open_fs(str(tmp_path)), LocalFS)


def test_open_fs_s3(monkeypatch):
    from moto import mock_aws

    from ocfl_lib.fs.s3 import S3FS

    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        fs = open_fs("s3://bucket/a/b")
    assert isinstance(fs, S3FS)
    assert (fs._bucket, fs._prefix) == ("bucket", "a/b")
