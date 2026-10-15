import json
import socket
from pathlib import Path

import pytest
from fakes import FakeBitcoinRPC, FakeSimpleProof
from typer.testing import CliRunner

from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.main import app, production_ports
from ocfl_lib import BitcoinRpcAuthError, BitcoinRpcTransientError

SECRETS = {
    "SIMPLEPROOF_ACCESS_TOKEN": "tok-SECRET-1",
    "SIMPLEPROOF_API_KEY": "key-SECRET-2",
    "BITCOIN_RPC_USER": "user-SECRET-3",
    "BITCOIN_RPC_PASSWORD": "pass-SECRET-4",
}


def _toml(zmq: str) -> str:
    return (
        '[simpleproof]\nbase_url = "https://sp.example"\nproof_category_id = 7\n\n'
        f'[bitcoin]\nrpc_url = "http://127.0.0.1:8332"\nzmq_endpoint = "{zmq}"\n'
    )


@pytest.fixture
def listener():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    yield f"tcp://127.0.0.1:{s.getsockname()[1]}"
    s.close()


@pytest.fixture
def cfg_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listener: str) -> Path:
    d = tmp_path / "cfg"
    d.mkdir()
    (d / "config.toml").write_text(_toml(listener))
    env = d / ".env"
    env.write_text("".join(f"{k}={v}\n" for k, v in SECRETS.items()))
    env.chmod(0o600)
    monkeypatch.setenv("OCFL_CONFIG", str(d / "config.toml"))
    monkeypatch.setenv("OCFL_ENV_FILE", str(env))
    monkeypatch.setenv("OCFL_STAGING_ROOT", str(tmp_path / "staging"))
    for k in (*SECRETS, "OCFL_ROOT"):
        monkeypatch.delenv(k, raising=False)
    return d


def _run(*args: str, sp=None, btc=None):
    ports = production_ports()
    ports.sp_client_factory = lambda cfg: sp if sp is not None else FakeSimpleProof()
    ports.btc_rpc_factory = lambda cfg: btc if btc is not None else FakeBitcoinRPC()
    return CliRunner().invoke(app, list(args), obj=ports)


def _checks(result) -> dict[str, dict]:
    return {c["name"]: c for c in json.loads(result.stdout)["checks"]}


class _Btc:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def getblockchaininfo(self):
        raise self.exc


class TestPath:
    def test_paths(self, cfg_dir: Path, tmp_path: Path) -> None:
        r = _run("--json", "config", "path")
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert json.loads(r.stdout) == {
            "config": str(cfg_dir / "config.toml"),
            "config_exists": True,
            "env_file": str(cfg_dir / ".env"),
            "env_file_exists": True,
            "staging_root": str(tmp_path / "staging"),
        }
        r = _run("config", "path")
        assert f"config: {cfg_dir / 'config.toml'}\n" in r.stdout

    def test_missing_config_is_marked(self, cfg_dir: Path) -> None:
        (cfg_dir / "config.toml").unlink()
        r = _run("config", "path")
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert "(missing)" in r.stdout


class TestBrokenConfig:
    @pytest.mark.parametrize(
        ("args", "code"),
        [
            (("config", "path"), ExitCode.SUCCESS),
            (("config", "show"), ExitCode.CONFIG),
            (("config", "check"), ExitCode.CONFIG),
            (("ls",), ExitCode.CONFIG),
            (("stage", "ls"), ExitCode.CONFIG),
            (("stage", "batch", "ls"), ExitCode.CONFIG),
            (("ls", "--help"), ExitCode.SUCCESS),
            (("ls", "--bogus"), ExitCode.USAGE),
        ],
    )
    def test_exit_code(self, cfg_dir: Path, args: tuple[str, ...], code) -> None:
        (cfg_dir / "config.toml").write_text("not = [toml")
        assert _run(*args).exit_code == code

    def test_stage_fails_before_making_the_staging_root(
        self, cfg_dir: Path, tmp_path: Path
    ) -> None:
        (cfg_dir / "config.toml").write_text("not = [toml")
        assert _run("stage", "ls").exit_code == ExitCode.CONFIG
        assert not (tmp_path / "staging").exists()


class TestShow:
    def test_redacts_every_secret(self, cfg_dir: Path) -> None:
        text = _run("config", "show")
        doc = _run("--json", "config", "show")
        assert text.exit_code == doc.exit_code == ExitCode.SUCCESS, text.output
        data = json.loads(doc.stdout)
        assert data["simpleproof"]["access_token"] == "<redacted>"
        assert data["bitcoin"]["rpc_user"] == "<redacted>"
        assert data["bitcoin"]["confirmations"] == 3
        assert "simpleproof.proof_category_id = 7" in text.stdout
        for out in (text.stdout + text.stderr, doc.stdout + doc.stderr):
            for secret in SECRETS.values():
                assert secret not in out


class TestCheck:
    def test_all_ok(self, cfg_dir: Path) -> None:
        r = _run("--json", "config", "check")
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert json.loads(r.stdout)["ok"] is True
        assert list(_checks(r)) == [
            "config",
            "env_file",
            "simpleproof",
            "bitcoin_rpc",
            "bitcoin_zmq",
        ]
        text = _run("config", "check")
        assert text.exit_code == ExitCode.SUCCESS
        assert text.stdout.count("ok  ") == 5
        for secret in SECRETS.values():
            assert secret not in text.stdout + r.stdout

    def test_simpleproof_denied_is_exit_6(self, cfg_dir: Path) -> None:
        r = _run("--json", "config", "check", sp=FakeSimpleProof(access_denied=True))
        assert r.exit_code == ExitCode.CONFIG, r.output
        assert _checks(r)["simpleproof"]["error_class"] == "config"
        assert json.loads(r.stderr)["error"]["code"] == "config_check_failed"

    def test_rpc_auth_is_exit_6(self, cfg_dir: Path) -> None:
        btc = _Btc(BitcoinRpcAuthError("Bitcoin RPC auth failed: HTTP 401"))
        r = _run("--json", "config", "check", btc=btc)
        assert r.exit_code == ExitCode.CONFIG, r.output
        assert _checks(r)["bitcoin_rpc"]["ok"] is False

    def test_rpc_down_is_exit_1(self, cfg_dir: Path) -> None:
        btc = _Btc(BitcoinRpcTransientError("connection refused"))
        r = _run("--json", "config", "check", btc=btc)
        assert r.exit_code == ExitCode.GENERIC, r.output
        assert _checks(r)["bitcoin_rpc"]["error_class"] == "service"
        assert json.loads(r.stderr)["error"]["code"] == "service_check_failed"

    def test_zmq_ipc_socket_missing_is_exit_1(self, cfg_dir: Path) -> None:
        missing = cfg_dir / "no.sock"
        (cfg_dir / "config.toml").write_text(_toml(f"ipc://{missing}"))
        r = _run("--json", "config", "check")
        assert r.exit_code == ExitCode.GENERIC, r.output
        assert str(missing) in _checks(r)["bitcoin_zmq"]["detail"]

    def test_zmq_ipc_socket_present(self, cfg_dir: Path) -> None:
        sock = cfg_dir / "zmq.sock"
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(sock))
        try:
            (cfg_dir / "config.toml").write_text(_toml(f"ipc://{sock}"))
            assert _run("config", "check").exit_code == ExitCode.SUCCESS
        finally:
            s.close()

    def test_open_env_file_is_exit_6(self, cfg_dir: Path) -> None:
        (cfg_dir / ".env").chmod(0o644)
        r = _run("--json", "config", "check")
        assert r.exit_code == ExitCode.CONFIG, r.output
        checks = _checks(r)
        assert checks["env_file"]["ok"] is False and checks["config"]["ok"] is False
        assert set(checks) == {"config", "env_file"}

    def test_sections_not_configured_are_ok(self, cfg_dir: Path) -> None:
        (cfg_dir / "config.toml").write_text("")
        r = _run("--json", "config", "check")
        assert r.exit_code == ExitCode.SUCCESS, r.output
        assert _checks(r)["simpleproof"]["detail"] == "not configured"
