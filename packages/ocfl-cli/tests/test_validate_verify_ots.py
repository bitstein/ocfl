import hashlib
import json
from pathlib import Path

import pytest
from fakes import FakeBitcoinRPC
from runner_helpers import runner_obj
from typer.testing import CliRunner

from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app
from ocfl_lib import (
    BitcoinRpcTransientError,
    LocalFS,
    Spec,
    StorageRoot,
    get_layout,
)
from tests.helpers.ots import build_attestation

_EXT_NAME = "NNNN-ots-timestamps"
DIGEST_BLOCK_HEIGHT = 800_000
# Content proofs sit in an earlier block than the inventory proof: one block
# has one merkle root, and every proof is checked against Bitcoin.
CONTENT_BLOCK_HEIGHT = DIGEST_BLOCK_HEIGHT - 10
MIN_CONFS = 3

_SP_OK = """\
[simpleproof]
base_url = "https://stage.example"
proof_category_id = 641
"""

_BTC_OK = """\
[bitcoin]
rpc_url = "http://127.0.0.1:8332"
zmq_endpoint = "tcp://127.0.0.1:28332"
confirmations = 3
"""


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


def _inject_fake_btc(runner: CliRunner, fake: FakeBitcoinRPC) -> None:
    runner_obj(runner)["btc_rpc_factory"] = lambda cfg: fake


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


def _anchor_content(rpc: FakeBitcoinRPC) -> None:
    _grow_chain_to(rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
    digest_hex = hashlib.sha256(b"hello world").hexdigest()
    _register_attested(rpc, digest_hex, CONTENT_BLOCK_HEIGHT)


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
            ots_bytes = build_attestation(
                file_sha256, block_height=CONTENT_BLOCK_HEIGHT
            )
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


class TestDefaultOnVerifyOts:
    def test_default_on_walks_chain_with_valid_attestation(
        self,
        runner: CliRunner,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)

        calls: list[str] = []

        class _SpyRPC:
            def __getattr__(self, name: str) -> object:
                calls.append(name)
                return getattr(btc_fake.rpc, name)

        _inject_fake_btc(runner, _SpyRPC())  # ty: ignore[invalid-argument-type]

        root = _make_root_with_ots_object(tmp_path, obj_id="obj-test")

        digest_hex = hashlib.sha256(b"hello world").hexdigest()
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        _register_attested(btc_fake.rpc, digest_hex, CONTENT_BLOCK_HEIGHT)
        inv_path = root / "obj-test" / "inventory.json"
        inv_digest_hex = hashlib.sha256(inv_path.read_bytes()).hexdigest()
        _register_attested(btc_fake.rpc, inv_digest_hex)

        r = runner.invoke(app, ["--root", str(root), "--json", "validate", "obj-test"])
        assert r.exit_code == ExitCode.SUCCESS, r.output
        data = json.loads(r.stdout)
        assert data["valid"] is True
        assert data["errors"] == []
        assert calls, (
            "default-on must walk the chain via the injected btc factory; "
            "no RPC methods were invoked"
        )


class TestOtsLocalSkipsWalk:
    def test_ots_local_skips_network_walk(
        self,
        runner: CliRunner,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
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
                "--ots=local",
            ],
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        data = json.loads(r.stdout)
        assert data["valid"] is True
        codes = {e["code"] for e in data["errors"]}
        assert "E906" not in codes

    def test_omits_ots_section_when_ots_local(
        self,
        runner: CliRunner,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_btc(runner, btc_fake.rpc)

        root = _make_root_with_ots_object(tmp_path, obj_id="obj-test")

        r = runner.invoke(
            app,
            [
                "--root",
                str(root),
                "validate",
                "obj-test",
                "--ots=local",
            ],
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert "Verifying OTS attestations" not in r.stderr


class TestVerifyOtsProgress:
    def test_emits_per_version_ots_results_by_default(
        self,
        runner: CliRunner,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_btc(runner, btc_fake.rpc)

        root = _make_root_with_ots_object(tmp_path, obj_id="obj-test")
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        digest_hex = hashlib.sha256(b"hello world").hexdigest()
        _register_attested(btc_fake.rpc, digest_hex, CONTENT_BLOCK_HEIGHT)
        inv_path = root / "obj-test" / "inventory.json"
        inv_digest_hex = hashlib.sha256(inv_path.read_bytes()).hexdigest()
        _register_attested(btc_fake.rpc, inv_digest_hex)

        r = runner.invoke(app, ["--root", str(root), "validate", "obj-test"])

        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert "Verifying OTS attestations (min 3 confirmations)" in r.stderr
        assert "  v1 (inventory.json.v1.ots): confirmed" in r.stderr
        assert "block 800000" in r.stderr
        assert "  v1 (file.txt.ots): confirmed" in r.stderr
        assert f"block {CONTENT_BLOCK_HEIGHT}" in r.stderr


class TestMissingBitcoinConfig:
    def test_auto_without_bitcoin_config_checks_locally(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch, body=_SP_OK)
        root = _make_root_with_ots_object(tmp_path, obj_id="obj-test")

        r = runner.invoke(app, ["--root", str(root), "validate", "obj-test"])

        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert "local checks only" in r.stderr

    def test_chain_without_bitcoin_config_raises_config_error(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch, body=_SP_OK)
        root = _make_root_with_ots_object(tmp_path, obj_id="obj-test")

        r = runner.invoke(
            app, ["--root", str(root), "validate", "obj-test", "--ots=chain"]
        )

        assert r.exit_code == ExitCode.CONFIG, r.output
        assert "[bitcoin]" in r.output


class TestStructuralAlwaysRuns:
    def test_structural_validation_always_runs_with_ots_local(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)

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
                "--ots=local",
            ],
        )
        assert r.exit_code == ExitCode.GENERIC, r.output
        data = json.loads(r.stdout)
        codes = {e["code"] for e in data["errors"]}
        assert "E902" in codes, (
            f"structural E902 must surface with --ots=local; got codes={codes}"
        )


class TestVerifyTimeMismatchE906:
    def test_default_on_wrong_merkle_root_reports_e906(
        self,
        runner: CliRunner,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_btc(runner, btc_fake.rpc)

        root = _make_root_with_ots_object(tmp_path, obj_id="obj-test")

        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        btc_fake.rpc.register_merkle_root(DIGEST_BLOCK_HEIGHT, "00" * 32)

        r = runner.invoke(app, ["--root", str(root), "--json", "validate", "obj-test"])
        assert r.exit_code == ExitCode.GENERIC, r.output
        data = json.loads(r.stdout)
        codes = {e["code"] for e in data["errors"]}
        assert "E906" in codes, (
            f"verify-time merkle mismatch must surface E906; got codes={codes}"
        )

    def test_default_on_corrupt_inventory_ots_reports_e905_json(
        self,
        runner: CliRunner,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_btc(runner, btc_fake.rpc)

        root = _make_root_with_ots_object(tmp_path, obj_id="obj-test")
        (
            root / "obj-test" / "extensions" / _EXT_NAME / "inventory.json.v1.ots"
        ).write_bytes(b"not an ots file")

        r = runner.invoke(app, ["--root", str(root), "--json", "validate", "obj-test"])

        assert r.exit_code == ExitCode.GENERIC, r.output
        assert isinstance(r.exception, SystemExit)
        data = json.loads(r.stdout)
        codes = {e["code"] for e in data["errors"]}
        assert "E905" in codes
        assert "E906" not in codes

    def test_default_on_bitcoin_rpc_failure_fails_without_e906_json(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:

        class DownRPC(FakeBitcoinRPC):
            def getblockhash(self, height: int) -> str:
                raise BitcoinRpcTransientError("Bitcoin RPC transport error")

        _write_config(tmp_path, monkeypatch)
        _inject_fake_btc(runner, DownRPC())

        root = _make_root_with_ots_object(tmp_path, obj_id="obj-test")

        r = runner.invoke(app, ["--root", str(root), "--json", "validate", "obj-test"])

        assert r.exit_code == ExitCode.GENERIC, r.output
        assert isinstance(r.exception, SystemExit)
        assert "E906" not in r.stdout
        assert "Bitcoin RPC transport error" in r.stderr

    def test_default_on_bitcoin_rpc_failure_reports_human_error(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:

        class DownRPC(FakeBitcoinRPC):
            def getblockhash(self, height: int) -> str:
                raise BitcoinRpcTransientError("Bitcoin RPC transport error")

        _write_config(tmp_path, monkeypatch)
        _inject_fake_btc(runner, DownRPC())

        root = _make_root_with_ots_object(tmp_path, obj_id="obj-test")

        r = runner.invoke(app, ["--root", str(root), "validate", "obj-test"])

        assert r.exit_code == ExitCode.GENERIC, r.output
        assert isinstance(r.exception, SystemExit)
        combined = r.output + (r.stderr or "")
        assert "cannot verify OTS attestations" in combined
        assert "Bitcoin RPC transport error" in combined
        assert "[E906]" not in combined
        assert "Traceback" not in combined


class TestVerifyPendingW903:
    def test_default_on_missing_inventory_ots_skips_verify_time_warning(
        self,
        runner: CliRunner,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_btc(runner, btc_fake.rpc)

        root = _make_root_with_ots_object(
            tmp_path,
            obj_id="obj-test",
            include_inventory_v1_ots=False,
        )

        _anchor_content(btc_fake.rpc)

        r = runner.invoke(app, ["--root", str(root), "--json", "validate", "obj-test"])

        assert r.exit_code == ExitCode.GENERIC, r.output
        data = json.loads(r.stdout)
        assert data["valid"] is False
        error_codes = {e["code"] for e in data["errors"]}
        warning_codes = {w["code"] for w in data["warnings"]}
        assert "E904" in error_codes
        assert "E906" not in error_codes
        assert "W903" not in warning_codes

    def test_default_on_missing_version_inventory_skips_verify_time_warning(
        self,
        runner: CliRunner,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_btc(runner, btc_fake.rpc)

        root = _make_root_with_ots_object(tmp_path, obj_id="obj-test")
        (root / "obj-test" / "v1" / "inventory.json").unlink()

        _anchor_content(btc_fake.rpc)

        r = runner.invoke(app, ["--root", str(root), "--json", "validate", "obj-test"])

        # The extension requires vN/inventory.json (E909); OCFL only warns (W010).
        assert r.exit_code == ExitCode.GENERIC, r.output
        data = json.loads(r.stdout)
        assert data["valid"] is False
        error_codes = {e["code"] for e in data["errors"]}
        warning_codes = {w["code"] for w in data["warnings"]}
        assert "W010" in warning_codes
        assert "E909" in error_codes
        assert "E906" not in error_codes
        assert "W903" not in warning_codes

    def test_default_on_pending_inventory_proof_is_w902_not_w903(
        self,
        runner: CliRunner,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_btc(runner, btc_fake.rpc)

        root = _make_root_with_ots_object(tmp_path, obj_id="obj-test")
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        digest_hex = hashlib.sha256(b"hello world").hexdigest()
        _register_attested(btc_fake.rpc, digest_hex, CONTENT_BLOCK_HEIGHT)
        inv_path = root / "obj-test" / "inventory.json"
        inv_digest = hashlib.sha256(inv_path.read_bytes()).digest()
        (
            root / "obj-test" / "extensions" / _EXT_NAME / "inventory.json.v1.ots"
        ).write_bytes(
            build_attestation(inv_digest, pending_uri="https://a.pool.example")
        )

        r = runner.invoke(app, ["--root", str(root), "--json", "validate", "obj-test"])

        assert r.exit_code == ExitCode.SUCCESS, r.output
        data = json.loads(r.stdout)
        assert data["valid"] is True
        assert data.keys() == {"valid", "errors", "warnings", "info"}
        assert data["errors"] == []
        warning_codes = {w["code"] for w in data["warnings"]}
        assert {"W004", "W005", "W902"}.issubset(warning_codes)
        assert "W903" not in warning_codes

    def test_default_on_insufficient_confirmations_reports_w903(
        self,
        runner: CliRunner,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_btc(runner, btc_fake.rpc)

        root = _make_root_with_ots_object(tmp_path, obj_id="obj-test")
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT)
        content_hex = hashlib.sha256(b"hello world").hexdigest()
        _register_attested(btc_fake.rpc, content_hex, CONTENT_BLOCK_HEIGHT)
        inv_path = root / "obj-test" / "inventory.json"
        inv_digest_hex = hashlib.sha256(inv_path.read_bytes()).hexdigest()
        _register_attested(btc_fake.rpc, inv_digest_hex)

        r = runner.invoke(app, ["--root", str(root), "--json", "validate", "obj-test"])

        assert r.exit_code == ExitCode.SUCCESS, r.output
        data = json.loads(r.stdout)
        assert data["valid"] is True
        w903 = [w for w in data["warnings"] if w["code"] == "W903"]
        assert len(w903) == 1
        assert "inventory.json.v1.ots" in w903[0]["message"]

    def test_default_on_missing_block_reports_w903(
        self,
        runner: CliRunner,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_btc(runner, btc_fake.rpc)

        root = _make_root_with_ots_object(tmp_path, obj_id="obj-test")
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT - 1)
        content_hex = hashlib.sha256(b"hello world").hexdigest()
        _register_attested(btc_fake.rpc, content_hex, CONTENT_BLOCK_HEIGHT)

        r = runner.invoke(app, ["--root", str(root), "--json", "validate", "obj-test"])

        assert r.exit_code == ExitCode.SUCCESS, r.output
        data = json.loads(r.stdout)
        assert data["valid"] is True
        w903 = [w for w in data["warnings"] if w["code"] == "W903"]
        assert len(w903) == 1
        assert "inventory.json.v1.ots" in w903[0]["message"]


@pytest.mark.bitcoin
class TestRegtestVerifyOtsEndToEnd:
    def test_real_rpc_walks_real_attestation_chain(
        self,
        runner: CliRunner,
        bitcoind_regtest,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from tests.helpers.ots import build_regtest_attestation
        from tests.helpers.regtest import bitcoin_config_toml, bury_to_confs

        cfg_body = _SP_OK + bitcoin_config_toml(bitcoind_regtest, confirmations=3)
        monkeypatch.setenv("BITCOIN_RPC_USER", bitcoind_regtest.rpc_user)
        monkeypatch.setenv("BITCOIN_RPC_PASSWORD", bitcoind_regtest.rpc_password)
        _write_config(tmp_path, monkeypatch, body=cfg_body)

        file_bytes = b"hello world"
        file_sha256 = hashlib.sha256(file_bytes).digest()
        content_ots, content_height, _ = build_regtest_attestation(
            bitcoind_regtest, file_sha256
        )

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

        inv_ots, inv_height, _ = build_regtest_attestation(bitcoind_regtest, inv_digest)

        bury_to_confs(
            bitcoind_regtest,
            max(content_height, inv_height),
            target_confs=MIN_CONFS,
            wallet="ocfltest",
        )

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

        r = runner.invoke(
            app, ["--root", str(root), "--json", "validate", "obj-regtest"]
        )
        assert r.exit_code == ExitCode.SUCCESS, r.output
        data = json.loads(r.stdout)
        assert data["valid"] is True, data
        assert data["errors"] == []


class TestRootLevelChain:
    def _bad_chain_root(self, runner, btc_fake, tmp_path, monkeypatch) -> Path:
        _write_config(tmp_path, monkeypatch)
        _inject_fake_btc(runner, btc_fake.rpc)
        root = _make_root_with_ots_object(tmp_path, obj_id="obj-test")
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        btc_fake.rpc.register_merkle_root(DIGEST_BLOCK_HEIGHT, "00" * 32)
        return root

    @pytest.mark.parametrize("flag", [[], ["--ots=auto"], ["--ots=chain"]])
    def test_root_validate_walks_each_objects_chain(
        self,
        runner: CliRunner,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        flag: list[str],
    ) -> None:
        root = self._bad_chain_root(runner, btc_fake, tmp_path, monkeypatch)

        r = runner.invoke(app, ["--root", str(root), "--json", "validate", *flag])

        assert r.exit_code == ExitCode.GENERIC, r.output
        codes = {e["code"] for e in json.loads(r.stdout)["errors"]}
        assert "E906" in codes, codes

    def test_root_validate_local_skips_the_chain(
        self,
        runner: CliRunner,
        btc_fake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = self._bad_chain_root(runner, btc_fake, tmp_path, monkeypatch)

        r = runner.invoke(
            app, ["--root", str(root), "--json", "validate", "--ots=local"]
        )

        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert json.loads(r.stdout)["valid"] is True

    def test_lib_validate_root_takes_the_chain_check(
        self, btc_fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ocfl_cli.workflow.validation import chain_source
        from ocfl_lib import LocalFS, ots, validate_root

        root = _make_root_with_ots_object(tmp_path, obj_id="obj-test")
        _grow_chain_to(btc_fake.rpc, DIGEST_BLOCK_HEIGHT + MIN_CONFS - 1)
        btc_fake.rpc.register_merkle_root(DIGEST_BLOCK_HEIGHT, "00" * 32)
        chain = ots.ChainCheck(chain_source(btc_fake.rpc), MIN_CONFS)

        fs = LocalFS(root)
        assert validate_root(fs, "").is_valid
        report = validate_root(fs, "", chain=chain)
        assert "E906" in {f.code for f in report.errors}
