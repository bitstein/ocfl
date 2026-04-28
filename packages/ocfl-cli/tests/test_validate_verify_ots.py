"""Tests for ``ocfl validate --verify-ots`` default-on behavior.

Pinned by ``OTS workflow contract``:

- ``--verify-ots`` defaults ON when the OTS extension is present in the
  object; ``--no-verify-ots`` opts out (offline CI).
- Default-on requires ``[bitcoin]`` config; missing config raises
  ``ConfigError`` with an actionable hint mentioning ``[bitcoin]``
  (CLI exit 2 per ``error_taxonomy.exit_codes``).
- Default-on walks the attestation chain via ``verify_attestation`` and
  cross-checks the merkle root against the Bitcoin chain. A mismatch
  surfaces a new ``E906`` (verify-time mismatch) error and exits 1.
- Structural validation ALWAYS runs, regardless of
  ``--verify-ots``/``--no-verify-ots``.

Tests inject a ``FakeBitcoinRPC`` via ``runner.obj["btc_rpc_factory"]``
(see ``conftest.py::runner`` for the auto-attach mechanism).
"""

import hashlib
import json
from pathlib import Path

import pytest
from fakes import FakeBitcoinRPC
from typer.testing import CliRunner

from ocfl_cli.main import app
from ocfl_lib import LocalFS, Spec, StorageRoot, get_layout
from tests.helpers.ots import build_attestation

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_EXT_NAME = "NNNN-ots-timestamps"
DIGEST_BLOCK_HEIGHT = 800_000
MIN_CONFS = 3

_SP_OK = """\
[simpleproof]
base_url = "https://stage.example"
proof_category_id = 641
access_token = "tok"
api_key = "key"
"""

_BTC_OK = """\
[bitcoin]
rpc_url = "http://127.0.0.1:8332"
zmq_endpoint = "tcp://127.0.0.1:28332"
rpc_user = "u"
rpc_password = "p"
confirmations = 3
"""


# --------------------------------------------------------------------------- #
# Config helper
# --------------------------------------------------------------------------- #


def _write_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: str = _SP_OK + _BTC_OK,
) -> Path:
    cfg = tmp_path / "ocfl-config" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(body)
    monkeypatch.setenv("OCFL_CONFIG", str(cfg))
    return cfg


# --------------------------------------------------------------------------- #
# Bitcoin RPC seam injection
# --------------------------------------------------------------------------- #


def _inject_fake_btc(runner: CliRunner, fake: FakeBitcoinRPC) -> None:
    """Inject *fake* via ``runner.obj["btc_rpc_factory"]``."""
    runner.obj["btc_rpc_factory"] = lambda cfg: fake


def _grow_chain_to(rpc: FakeBitcoinRPC, height: int) -> None:
    needed = height - rpc.tip_height
    if needed > 0:
        rpc.extend(needed)


def _register_attested(
    rpc: FakeBitcoinRPC, digest_hex: str, height: int = DIGEST_BLOCK_HEIGHT
) -> None:
    """Register the merkle root in the fake chain so verify_attestation succeeds.

    ``build_attestation`` puts no ops between the leaf and the
    ``BitcoinBlockHeaderAttestation`` — attested merkle root (internal
    bytes) == file digest. bitcoind reports merkle in display
    (reversed) hex.
    """
    rpc.register_merkle_root(height, bytes.fromhex(digest_hex)[::-1].hex())


# --------------------------------------------------------------------------- #
# OCFL-with-OTS object builder (test-side helper, cribbed from
# ``packages/ocfl-lib/tests/test_validation_extension_ots.py::_build_ots_object``).
# --------------------------------------------------------------------------- #


def _hex(alg: str, data: bytes) -> str:
    return hashlib.new(alg, data).hexdigest()


def _build_ots_object(
    obj_dir: Path,
    *,
    obj_id: str = "http://example.org/obj1",
    files: dict[str, bytes] | None = None,
    include_companions: bool = True,
    include_extension_dir: bool = True,
    include_inventory_v1_ots: bool = True,
    digest_algorithm: str = "sha256",
) -> Path:
    """Write a minimal SHA-256 OCFL 1.1 object with the OTS extension."""
    files = {"file.txt": b"hello world"} if files is None else files

    manifest: dict[str, list[str]] = {}
    state: dict[str, list[str]] = {}

    def _add(content_path: str, state_path: str, data: bytes) -> None:
        d = _hex(digest_algorithm, data)
        manifest.setdefault(d, []).append(content_path)
        state.setdefault(d, []).append(state_path)

    companion_bytes: dict[str, bytes] = {}
    for name, data in files.items():
        _add(f"v1/content/{name}", name, data)
        if include_companions:
            file_sha256 = hashlib.sha256(data).digest()
            ots_bytes = build_attestation(file_sha256, block_height=DIGEST_BLOCK_HEIGHT)
            companion_bytes[name] = ots_bytes
            _add(f"v1/content/{name}.ots", f"{name}.ots", ots_bytes)

    inv = {
        "id": obj_id,
        "type": "https://ocfl.io/1.1/spec/#inventory",
        "digestAlgorithm": digest_algorithm,
        "head": "v1",
        "manifest": manifest,
        "versions": {
            "v1": {
                "created": "2024-01-01T00:00:00Z",
                "state": state,
                "message": "init",
                "user": {"name": "t", "address": "mailto:t@e.org"},
            },
        },
    }
    inv_bytes = json.dumps(inv, sort_keys=True, separators=(",", ":")).encode()
    inv_digest = _hex(digest_algorithm, inv_bytes)

    obj_dir.mkdir(parents=True, exist_ok=True)
    (obj_dir / "0=ocfl_object_1.1").write_text("ocfl_object_1.1\n")
    (obj_dir / "inventory.json").write_bytes(inv_bytes)
    (obj_dir / f"inventory.json.{digest_algorithm}").write_text(
        f"{inv_digest} inventory.json\n"
    )

    v1 = obj_dir / "v1"
    v1.mkdir()
    (v1 / "inventory.json").write_bytes(inv_bytes)
    (v1 / f"inventory.json.{digest_algorithm}").write_text(
        f"{inv_digest} inventory.json\n"
    )
    content = v1 / "content"
    content.mkdir()
    for name, data in files.items():
        (content / name).write_bytes(data)
        if include_companions:
            (content / f"{name}.ots").write_bytes(companion_bytes[name])

    if include_extension_dir:
        ext_dir = obj_dir / "extensions" / _EXT_NAME
        ext_dir.mkdir(parents=True)
        (ext_dir / "config.json").write_text(json.dumps({"extensionName": _EXT_NAME}))
        if include_inventory_v1_ots:
            inv_sha256 = hashlib.sha256(inv_bytes).digest()
            (ext_dir / "inventory.json.v1.ots").write_bytes(
                build_attestation(inv_sha256, block_height=DIGEST_BLOCK_HEIGHT)
            )

    return obj_dir


def _make_root_with_ots_object(
    tmp_path: Path,
    *,
    obj_id: str = "obj-test",
    include_companions: bool = True,
    include_extension_dir: bool = True,
    include_inventory_v1_ots: bool = True,
) -> Path:
    """Init a flat-direct SHA-256 storage root with one OTS-extended object.

    ``flat_direct`` requires the id to be a valid single path component
    (no colon, no slash), so callers should use e.g. ``"obj-test"``.
    """
    root = tmp_path / "root"
    root.mkdir()
    fs = LocalFS(root)
    layout = get_layout("0002-flat-direct-storage-layout")
    StorageRoot.init(fs, fs, spec=Spec.V1_1, layout=layout)
    obj_dir = root / obj_id  # flat-direct: id == single path component
    _build_ots_object(
        obj_dir,
        obj_id=obj_id,
        include_companions=include_companions,
        include_extension_dir=include_extension_dir,
        include_inventory_v1_ots=include_inventory_v1_ots,
    )
    return root


# =========================================================================== #
# Test 1: default-on walks the chain (passes when correct merkle registered)
# =========================================================================== #


class TestDefaultOnVerifyOts:
    def test_default_on_walks_chain_with_valid_attestation(
        self,
        runner: CliRunner,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Extension present + default-on + correct merkle registered → exit 0."""
        _write_config(tmp_path, monkeypatch)

        # Track whether the cmd actually consults the RPC — the walk must
        # happen by default when the OTS extension is present.
        calls: list[str] = []

        class _SpyRPC:
            def __getattr__(self, name: str) -> object:
                calls.append(name)
                return getattr(btc_fake.rpc, name)

        _inject_fake_btc(runner, _SpyRPC())  # type: ignore[arg-type]

        root = _make_root_with_ots_object(tmp_path, obj_id="obj-test")

        # Bury the attested height past MIN_CONFS, register matching merkle.
        digest_hex = hashlib.sha256(b"hello world").hexdigest()
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        _register_attested(btc_fake.rpc, digest_hex)
        # Inventory v1 attestation also needs its merkle in chain.
        inv_path = root / "obj-test" / "inventory.json"
        inv_digest_hex = hashlib.sha256(inv_path.read_bytes()).hexdigest()
        _register_attested(btc_fake.rpc, inv_digest_hex)

        r = runner.invoke(app, ["--root", str(root), "--json", "validate", "obj-test"])
        assert r.exit_code == 0, r.output
        data = json.loads(r.stdout)
        assert data["valid"] is True
        assert data["errors"] == []
        # Walk actually happened: RPC was consulted at least once.
        assert calls, (
            "default-on must walk the chain via the injected btc factory; "
            "no RPC methods were invoked"
        )


# =========================================================================== #
# Test 2: --no-verify-ots skips network walk (offline CI)
# =========================================================================== #


class TestNoVerifyOtsSkipsWalk:
    def test_no_verify_ots_skips_network_walk(
        self,
        runner: CliRunner,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--no-verify-ots`` skips the chain walk entirely.

        No merkle roots registered, chain at default height (genesis-only).
        Default-on would fail; ``--no-verify-ots`` must succeed.
        """
        _write_config(tmp_path, monkeypatch)
        # Inject the fake but DO NOT register any merkle / extend chain;
        # if the cmd touches the RPC despite --no-verify-ots, verification
        # will fail and surface the bug.
        _inject_fake_btc(runner, btc_fake.rpc)

        root = _make_root_with_ots_object(tmp_path, obj_id="obj-test")

        r = runner.invoke(
            app,
            [
                "--root",
                str(root),
                "--json",
                "validate",
                "obj-test",
                "--no-verify-ots",
            ],
        )
        assert r.exit_code == 0, r.output
        data = json.loads(r.stdout)
        assert data["valid"] is True
        # No verify-time codes (E906) when the walk is skipped.
        codes = {e["code"] for e in data["errors"]}
        assert "E906" not in codes


# =========================================================================== #
# Test 3: missing [bitcoin] config + default-on → ConfigError, exit 2
# =========================================================================== #


class TestMissingBitcoinConfig:
    def test_missing_bitcoin_config_default_on_raises_config_error(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Extension present + default-on + no ``[bitcoin]`` section → exit 2.

        The error message must mention ``[bitcoin]`` so the operator
        knows which section to add. Exit code 2 per
        ``error_taxonomy.exit_codes`` (config error).
        """
        # Config has [simpleproof] only; no [bitcoin] section.
        _write_config(tmp_path, monkeypatch, body=_SP_OK)

        root = _make_root_with_ots_object(tmp_path, obj_id="obj-test")

        r = runner.invoke(app, ["--root", str(root), "validate", "obj-test"])
        assert r.exit_code == 2, r.output
        # Hint must reference the missing section by name.
        combined = (r.stdout or "") + (r.stderr or "") + (r.output or "")
        assert "[bitcoin]" in combined, (
            f"ConfigError hint must mention `[bitcoin]`; got: {combined!r}"
        )


# =========================================================================== #
# Test 4: structural validation always runs (E902 with --no-verify-ots)
# =========================================================================== #


class TestStructuralAlwaysRuns:
    def test_structural_validation_always_runs_with_no_verify_ots(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--no-verify-ots`` skips the chain walk but NOT the structural
        checks: a missing companion file still surfaces E902 and exits 1.
        """
        _write_config(tmp_path, monkeypatch)

        # Object has the OTS extension config but NO `.ots` companions —
        # structural step 4 must report E902 (companion-missing).
        root = _make_root_with_ots_object(
            tmp_path,
            obj_id="obj-test",
            include_companions=False,
        )

        r = runner.invoke(
            app,
            [
                "--root",
                str(root),
                "--json",
                "validate",
                "obj-test",
                "--no-verify-ots",
            ],
        )
        assert r.exit_code == 1, r.output
        data = json.loads(r.stdout)
        codes = {e["code"] for e in data["errors"]}
        assert "E902" in codes, (
            f"structural E902 must surface with --no-verify-ots; got codes={codes}"
        )


# =========================================================================== #
# Test 5: default-on with WRONG merkle root → E906 (verify-time mismatch)
# =========================================================================== #


class TestVerifyTimeMismatchE906:
    def test_default_on_wrong_merkle_root_reports_e906(
        self,
        runner: CliRunner,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Default-on walks the chain and finds a bogus merkle at the
        attested height → reports new error code ``E906`` and exits 1.
        """
        _write_config(tmp_path, monkeypatch)
        _inject_fake_btc(runner, btc_fake.rpc)

        root = _make_root_with_ots_object(tmp_path, obj_id="obj-test")

        # Chain buried past MIN_CONFS, but register a WRONG merkle root
        # (zeros) at the attested height — verify_attestation must fail.
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        btc_fake.rpc.register_merkle_root(DIGEST_BLOCK_HEIGHT, "00" * 32)

        r = runner.invoke(app, ["--root", str(root), "--json", "validate", "obj-test"])
        assert r.exit_code == 1, r.output
        data = json.loads(r.stdout)
        codes = {e["code"] for e in data["errors"]}
        assert "E906" in codes, (
            f"verify-time merkle mismatch must surface E906; got codes={codes}"
        )


# =========================================================================== #
# Layer-3: regtest end-to-end (skipped without --run-bitcoin)
# =========================================================================== #


@pytest.mark.bitcoin
class TestRegtestVerifyOtsEndToEnd:
    def test_real_rpc_walks_real_attestation_chain(
        self,
        runner: CliRunner,
        bitcoind_regtest,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end: real bitcoind regtest walks a real attestation chain.

        Builds an OCFL object with content + inventory ``.ots`` companions
        anchored to real regtest blocks (``OP_RETURN`` commit, real merkle
        path, real block header). Runs ``ocfl validate --verify-ots`` —
        production ``btc_rpc_factory`` path drives a real
        ``BitcoinRPCClient`` against the regtest node.
        """
        from tests.helpers.ots import build_regtest_attestation
        from tests.helpers.regtest import bitcoin_config_toml, bury_to_confs

        cfg_body = _SP_OK + bitcoin_config_toml(bitcoind_regtest, confirmations=3)
        _write_config(tmp_path, monkeypatch, body=cfg_body)

        # Real attestation for the content file.
        file_bytes = b"hello world"
        file_sha256 = hashlib.sha256(file_bytes).digest()
        content_ots, content_height, _ = build_regtest_attestation(
            bitcoind_regtest, file_sha256
        )

        # Build OCFL inventory referencing the content + its .ots companion.
        digest_alg = "sha256"
        manifest: dict[str, list[str]] = {}
        state: dict[str, list[str]] = {}

        def _add(content_path: str, state_path: str, data: bytes) -> None:
            d = hashlib.new(digest_alg, data).hexdigest()
            manifest.setdefault(d, []).append(content_path)
            state.setdefault(d, []).append(state_path)

        _add("v1/content/file.txt", "file.txt", file_bytes)
        _add("v1/content/file.txt.ots", "file.txt.ots", content_ots)

        inv = {
            "id": "obj-regtest",
            "type": "https://ocfl.io/1.1/spec/#inventory",
            "digestAlgorithm": digest_alg,
            "head": "v1",
            "manifest": manifest,
            "versions": {
                "v1": {
                    "created": "2026-04-25T00:00:00Z",
                    "state": state,
                    "message": "init",
                    "user": {"name": "t", "address": "mailto:t@e.org"},
                },
            },
        }
        inv_bytes = json.dumps(inv, sort_keys=True, separators=(",", ":")).encode()
        inv_digest = hashlib.sha256(inv_bytes).digest()

        # Real attestation for the inventory digest (separate block).
        inv_ots, inv_height, _ = build_regtest_attestation(bitcoind_regtest, inv_digest)

        # Bury both attestations past MIN_CONFS.
        bury_to_confs(
            bitcoind_regtest,
            max(content_height, inv_height),
            target_confs=MIN_CONFS,
            wallet="ocfltest",
        )

        # Materialize the OCFL object on disk.
        root = tmp_path / "root"
        root.mkdir()
        fs = LocalFS(root)
        layout = get_layout("0002-flat-direct-storage-layout")
        StorageRoot.init(fs, fs, spec=Spec.V1_1, layout=layout)
        obj_dir = root / "obj-regtest"
        obj_dir.mkdir()
        (obj_dir / "0=ocfl_object_1.1").write_text("ocfl_object_1.1\n")
        (obj_dir / "inventory.json").write_bytes(inv_bytes)
        (obj_dir / f"inventory.json.{digest_alg}").write_text(
            f"{inv_digest.hex()} inventory.json\n"
        )
        v1 = obj_dir / "v1"
        v1.mkdir()
        (v1 / "inventory.json").write_bytes(inv_bytes)
        (v1 / f"inventory.json.{digest_alg}").write_text(
            f"{inv_digest.hex()} inventory.json\n"
        )
        content = v1 / "content"
        content.mkdir()
        (content / "file.txt").write_bytes(file_bytes)
        (content / "file.txt.ots").write_bytes(content_ots)
        ext_dir = obj_dir / "extensions" / _EXT_NAME
        ext_dir.mkdir(parents=True)
        (ext_dir / "config.json").write_text(json.dumps({"extensionName": _EXT_NAME}))
        (ext_dir / "inventory.json.v1.ots").write_bytes(inv_ots)

        # Run validate; production btc_rpc_factory builds a real client from cfg.
        r = runner.invoke(
            app, ["--root", str(root), "--json", "validate", "obj-regtest"]
        )
        assert r.exit_code == 0, r.output
        data = json.loads(r.stdout)
        assert data["valid"] is True, data
        assert data["errors"] == []
