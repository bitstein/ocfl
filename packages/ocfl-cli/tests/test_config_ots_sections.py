import logging
import os
import sys
from pathlib import Path

import pytest
from ots_helpers import BTC_OK, SP_OK
from pydantic import SecretStr

from ocfl_cli.config import (
    BitcoinConfig,
    Config,
    ConfigError,
    SimpleProofConfig,
    load_config,
)


def test_ots_config_classes_reexported_from_domain_module() -> None:
    from ocfl_cli.config.ots import BitcoinConfig as DomainBitcoinConfig
    from ocfl_cli.config.ots import SimpleProofConfig as DomainSimpleProofConfig

    assert SimpleProofConfig is DomainSimpleProofConfig
    assert BitcoinConfig is DomainBitcoinConfig


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _load_requiring_ots_sections(path: Path | None = None) -> Config:
    cfg = load_config(path)
    cfg.require("simpleproof")
    cfg.require("bitcoin")
    return cfg


class TestValidLoad:
    def test_both_sections_populated(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "c.toml", SP_OK + BTC_OK)
        cfg = load_config(path)
        assert isinstance(cfg, Config)
        assert isinstance(cfg.simpleproof, SimpleProofConfig)
        assert cfg.simpleproof.base_url == "https://stage.example"
        assert cfg.simpleproof.proof_category_id == 641
        assert cfg.simpleproof.access_token.get_secret_value() == "tok"
        assert cfg.simpleproof.api_key.get_secret_value() == "key"
        assert cfg.simpleproof.timeout_s == 30

        assert isinstance(cfg.bitcoin, BitcoinConfig)
        assert cfg.bitcoin.rpc_url == "http://127.0.0.1:8332"
        assert cfg.bitcoin.zmq_endpoint == "tcp://127.0.0.1:28332"
        assert cfg.bitcoin.rpc_user == "u"
        assert cfg.bitcoin.rpc_password.get_secret_value() == "p"
        assert cfg.bitcoin.confirmations == 3
        assert cfg.bitcoin.rpc_timeout_s == 10

    def test_explicit_overrides(self, tmp_path: Path) -> None:
        body = (
            SP_OK.replace("proof_category_id = 641", "proof_category_id = 99")
            + "timeout_s = 60\n"
            + BTC_OK
            + "confirmations = 6\nrpc_timeout_s = 25\n"
        )
        path = _write(tmp_path / "c.toml", body)
        cfg = load_config(path)
        assert cfg.simpleproof is not None
        assert cfg.simpleproof.timeout_s == 60
        assert cfg.simpleproof.proof_category_id == 99
        assert cfg.bitcoin is not None
        assert cfg.bitcoin.confirmations == 6
        assert cfg.bitcoin.rpc_timeout_s == 25
        assert cfg.bitcoin.rpc_user == "u"
        assert cfg.bitcoin.rpc_password.get_secret_value() == "p"


class TestUrlSchemes:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1",
            "http://192.168.1.10",
            "http://100.64.0.42",
            "http://bitcoin.tail-abc.ts.net",
            "http://node.local",
            "https://stage.example",
        ],
    )
    def test_simpleproof_base_url_ok(self, url: str, tmp_path: Path) -> None:
        body = SP_OK.replace("https://stage.example", url) + BTC_OK
        path = _write(tmp_path / "c.toml", body)
        cfg = load_config(path)
        assert cfg.simpleproof is not None
        assert cfg.simpleproof.base_url == url

    @pytest.mark.parametrize(
        "bad_url",
        [
            "ftp://stage.example",
            "tcp://x",
            "file:///x",
            "stage.example",
            "http://stage.example",
            "http://bitcoin-node",
        ],
    )
    def test_simpleproof_base_url_rejects_non_http(
        self, bad_url: str, tmp_path: Path
    ) -> None:
        body = SP_OK.replace("https://stage.example", bad_url) + BTC_OK
        path = _write(tmp_path / "c.toml", body)
        with pytest.raises(ConfigError):
            _load_requiring_ots_sections(path)

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8332",
            "http://10.0.0.1:8332",
            "http://100.64.0.1:8332",
            "http://bitcoin.tail-abc.ts.net:8332",
            "http://node.local:8332",
            "https://node.example:8332",
        ],
    )
    def test_bitcoin_rpc_url_ok(self, url: str, tmp_path: Path) -> None:
        body = SP_OK + BTC_OK.replace("http://127.0.0.1:8332", url)
        path = _write(tmp_path / "c.toml", body)
        cfg = load_config(path)
        assert cfg.bitcoin is not None
        assert cfg.bitcoin.rpc_url == url

    @pytest.mark.parametrize(
        "bad_url",
        [
            "ftp://x",
            "tcp://127.0.0.1:8332",
            "127.0.0.1:8332",
            "http://node.example:8332",
            "http://bitcoin-node:8332",
        ],
    )
    def test_bitcoin_rpc_url_rejects_non_http(
        self, bad_url: str, tmp_path: Path
    ) -> None:
        body = SP_OK + BTC_OK.replace("http://127.0.0.1:8332", bad_url)
        path = _write(tmp_path / "c.toml", body)
        with pytest.raises(ConfigError):
            _load_requiring_ots_sections(path)

    @pytest.mark.parametrize(
        "endpoint",
        ["tcp://127.0.0.1:28332", "ipc:///tmp/zmq.sock"],
    )
    def test_zmq_endpoint_ok(self, endpoint: str, tmp_path: Path) -> None:
        body = SP_OK + BTC_OK.replace("tcp://127.0.0.1:28332", endpoint)
        path = _write(tmp_path / "c.toml", body)
        cfg = load_config(path)
        assert cfg.bitcoin is not None
        assert cfg.bitcoin.zmq_endpoint == endpoint

    @pytest.mark.parametrize(
        "bad",
        ["http://127.0.0.1:28332", "https://x", "127.0.0.1:28332"],
    )
    def test_zmq_endpoint_rejects_non_tcp_ipc(self, bad: str, tmp_path: Path) -> None:
        body = SP_OK + BTC_OK.replace("tcp://127.0.0.1:28332", bad)
        path = _write(tmp_path / "c.toml", body)
        with pytest.raises(ConfigError):
            _load_requiring_ots_sections(path)


class TestRpcAuth:
    def test_user_pass_ok(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "c.toml", SP_OK + BTC_OK)
        cfg = load_config(path)
        assert cfg.bitcoin is not None
        assert cfg.bitcoin.rpc_user == "u"
        assert cfg.bitcoin.rpc_password.get_secret_value() == "p"

    def test_missing_user_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = _write(
            tmp_path / ".env",
            "SIMPLEPROOF_ACCESS_TOKEN=dot-token\n"
            "SIMPLEPROOF_API_KEY=dot-key\n"
            "BITCOIN_RPC_PASSWORD=dot-pass\n",
        )
        os.chmod(env_file, 0o600)
        monkeypatch.setenv("OCFL_ENV_FILE", str(env_file))
        body = SP_OK + (
            "[bitcoin]\n"
            'rpc_url = "http://127.0.0.1:8332"\n'
            'zmq_endpoint = "tcp://127.0.0.1:28332"\n'
        )
        path = _write(tmp_path / "c.toml", body)
        with pytest.raises(ConfigError, match=r"bitcoin\.rpc_user"):
            _load_requiring_ots_sections(path)

    def test_missing_password_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = _write(
            tmp_path / ".env",
            "SIMPLEPROOF_ACCESS_TOKEN=dot-token\n"
            "SIMPLEPROOF_API_KEY=dot-key\n"
            "BITCOIN_RPC_USER=dot-user\n",
        )
        os.chmod(env_file, 0o600)
        monkeypatch.setenv("OCFL_ENV_FILE", str(env_file))
        body = SP_OK + (
            "[bitcoin]\n"
            'rpc_url = "http://127.0.0.1:8332"\n'
            'zmq_endpoint = "tcp://127.0.0.1:28332"\n'
        )
        path = _write(tmp_path / "c.toml", body)
        with pytest.raises(ConfigError, match=r"bitcoin\.rpc_password"):
            _load_requiring_ots_sections(path)

    def test_missing_both_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = _write(
            tmp_path / ".env",
            "SIMPLEPROOF_ACCESS_TOKEN=dot-token\nSIMPLEPROOF_API_KEY=dot-key\n",
        )
        os.chmod(env_file, 0o600)
        monkeypatch.setenv("OCFL_ENV_FILE", str(env_file))
        body = SP_OK + (
            "[bitcoin]\n"
            'rpc_url = "http://127.0.0.1:8332"\n'
            'zmq_endpoint = "tcp://127.0.0.1:28332"\n'
        )
        path = _write(tmp_path / "c.toml", body)
        with pytest.raises(ConfigError):
            _load_requiring_ots_sections(path)


def _btc_with_conf(n: int) -> str:
    return BTC_OK + f"confirmations = {n}\n"


class TestConfirmationsRange:
    @pytest.mark.parametrize("n", [0, -1, 101, 1000])
    def test_out_of_range_errors(self, n: int, tmp_path: Path) -> None:
        path = _write(tmp_path / "c.toml", SP_OK + _btc_with_conf(n))
        with pytest.raises(ConfigError):
            _load_requiring_ots_sections(path)

    @pytest.mark.parametrize("n", [1, 2])
    def test_below_three_warns(
        self, n: int, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = _write(tmp_path / "c.toml", SP_OK + _btc_with_conf(n))
        with caplog.at_level(logging.WARNING):
            cfg = load_config(path)
        assert cfg.bitcoin is not None
        assert cfg.bitcoin.confirmations == n
        warns = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and "confirm" in r.message.lower()
        ]
        assert warns, "expected WARNING about shallow confirmations"

    @pytest.mark.parametrize("n", [3, 6, 50, 100])
    def test_three_and_above_no_warn(
        self, n: int, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = _write(tmp_path / "c.toml", SP_OK + _btc_with_conf(n))
        with caplog.at_level(logging.WARNING):
            cfg = load_config(path)
        assert cfg.bitcoin is not None
        assert cfg.bitcoin.confirmations == n
        warns = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and "confirm" in r.message.lower()
        ]
        assert not warns, f"unexpected confirmations warning: {warns!r}"


class TestEnvOverride:
    def test_base_url_env_wins_over_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SIMPLEPROOF_BASE_URL", "https://prod.example")
        path = _write(tmp_path / "c.toml", SP_OK + BTC_OK)
        cfg = load_config(path)
        assert cfg.simpleproof is not None
        assert cfg.simpleproof.base_url == "https://prod.example"

    def test_access_token_env_wins_over_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SIMPLEPROOF_ACCESS_TOKEN", "ENVTOKEN")
        path = _write(tmp_path / "c.toml", SP_OK + BTC_OK)
        cfg = load_config(path)
        assert cfg.simpleproof is not None
        assert cfg.simpleproof.access_token.get_secret_value() == "ENVTOKEN"

    def test_api_key_env_wins_over_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SIMPLEPROOF_API_KEY", "ENVKEY")
        path = _write(tmp_path / "c.toml", SP_OK + BTC_OK)
        cfg = load_config(path)
        assert cfg.simpleproof is not None
        assert cfg.simpleproof.api_key.get_secret_value() == "ENVKEY"

    @pytest.mark.parametrize("var", ["SIMPLEPROOF_BASE_URL", "BITCOIN_RPC_URL"])
    def test_env_url_is_scheme_validated(
        self, var: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(var, "ftp://bad")
        path = _write(tmp_path / "c.toml", SP_OK + BTC_OK)
        with pytest.raises(ConfigError):
            _load_requiring_ots_sections(path)


class TestDotenv:
    def test_dotenv_file_is_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = _write(
            tmp_path / ".env",
            "SIMPLEPROOF_ACCESS_TOKEN=dot-token\n"
            "SIMPLEPROOF_API_KEY=dot-key\n"
            "BITCOIN_RPC_USER=dot-user\n"
            "BITCOIN_RPC_PASSWORD=dot-pass\n",
        )
        os.chmod(env_file, 0o600)
        monkeypatch.setenv("OCFL_ENV_FILE", str(env_file))
        cfg = load_config(_write(tmp_path / "c.toml", SP_OK + BTC_OK))
        assert cfg.simpleproof is not None
        assert cfg.bitcoin is not None
        assert cfg.simpleproof.access_token.get_secret_value() == "dot-token"
        assert cfg.simpleproof.api_key.get_secret_value() == "dot-key"
        assert cfg.bitcoin.rpc_user == "dot-user"
        assert cfg.bitcoin.rpc_password.get_secret_value() == "dot-pass"

    def test_process_env_overrides_dotenv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = _write(
            tmp_path / ".env",
            "SIMPLEPROOF_ACCESS_TOKEN=dot-token\n"
            "SIMPLEPROOF_API_KEY=dot-key\n"
            "BITCOIN_RPC_USER=dot-user\n"
            "BITCOIN_RPC_PASSWORD=dot-pass\n",
        )
        os.chmod(env_file, 0o600)
        monkeypatch.setenv("OCFL_ENV_FILE", str(env_file))
        monkeypatch.setenv("SIMPLEPROOF_ACCESS_TOKEN", "env-token")
        cfg = load_config(_write(tmp_path / "c.toml", SP_OK + BTC_OK))
        assert cfg.simpleproof is not None
        assert cfg.simpleproof.access_token.get_secret_value() == "env-token"

    def test_dotenv_overrides_toml_for_non_secret_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = _write(
            tmp_path / ".env",
            "SIMPLEPROOF_BASE_URL=https://dotenv.example\n"
            "SIMPLEPROOF_ACCESS_TOKEN=dot-token\n"
            "SIMPLEPROOF_API_KEY=dot-key\n"
            "BITCOIN_RPC_USER=dot-user\n"
            "BITCOIN_RPC_PASSWORD=dot-pass\n",
        )
        os.chmod(env_file, 0o600)
        monkeypatch.setenv("OCFL_ENV_FILE", str(env_file))
        cfg = load_config(_write(tmp_path / "c.toml", SP_OK + BTC_OK))
        assert cfg.simpleproof is not None
        assert cfg.simpleproof.base_url == "https://dotenv.example"

    def test_unknown_dotenv_key_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = _write(
            tmp_path / ".env",
            "SIMPLEPROOF_ACCESS_TOKEN=dot-token\n"
            "SIMPLEPROOF_API_KEY=dot-key\n"
            "SIMPLEPROOF_ACESS_TOKEN=typo\n"
            "BITCOIN_RPC_USER=dot-user\n"
            "BITCOIN_RPC_PASSWORD=dot-pass\n",
        )
        os.chmod(env_file, 0o600)
        monkeypatch.setenv("OCFL_ENV_FILE", str(env_file))
        with pytest.raises(ConfigError, match="Extra"):
            _load_requiring_ots_sections(_write(tmp_path / "c.toml", SP_OK + BTC_OK))

    def test_explicit_missing_dotenv_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing = tmp_path / "missing.env"
        monkeypatch.setenv("OCFL_ENV_FILE", str(missing))
        with pytest.raises(ConfigError, match="OCFL_ENV_FILE does not exist"):
            _load_requiring_ots_sections(_write(tmp_path / "c.toml", SP_OK + BTC_OK))


class TestBitcoinEnvOverride:
    def test_rpc_url_env_wins_over_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BITCOIN_RPC_URL", "http://10.0.0.1:8332")
        path = _write(tmp_path / "c.toml", SP_OK + BTC_OK)
        cfg = load_config(path)
        assert cfg.bitcoin is not None
        assert cfg.bitcoin.rpc_url == "http://10.0.0.1:8332"

    def test_rpc_user_env_wins_over_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BITCOIN_RPC_USER", "envuser")
        path = _write(tmp_path / "c.toml", SP_OK + BTC_OK)
        cfg = load_config(path)
        assert cfg.bitcoin is not None
        assert cfg.bitcoin.rpc_user == "envuser"

    def test_rpc_password_env_wins_over_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BITCOIN_RPC_PASSWORD", "ENVPASS")
        path = _write(tmp_path / "c.toml", SP_OK + BTC_OK)
        cfg = load_config(path)
        assert cfg.bitcoin is not None
        assert cfg.bitcoin.rpc_password.get_secret_value() == "ENVPASS"

    def test_zmq_endpoint_env_wins_over_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BITCOIN_ZMQ_ENDPOINT", "tcp://10.0.0.1:28332")
        path = _write(tmp_path / "c.toml", SP_OK + BTC_OK)
        cfg = load_config(path)
        assert cfg.bitcoin is not None
        assert cfg.bitcoin.zmq_endpoint == "tcp://10.0.0.1:28332"


class TestSecretStrAPI:
    def test_simpleproof_secrets_are_secret_str(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "c.toml", SP_OK + BTC_OK)
        cfg = load_config(path)
        assert cfg.simpleproof is not None
        assert isinstance(cfg.simpleproof.access_token, SecretStr)
        assert isinstance(cfg.simpleproof.api_key, SecretStr)
        assert cfg.simpleproof.access_token.get_secret_value() == "tok"
        assert cfg.simpleproof.api_key.get_secret_value() == "key"

    def test_bitcoin_password_is_secret_str(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "c.toml", SP_OK + BTC_OK)
        cfg = load_config(path)
        assert cfg.bitcoin is not None
        assert isinstance(cfg.bitcoin.rpc_password, SecretStr)
        assert cfg.bitcoin.rpc_password.get_secret_value() == "p"

    def test_str_of_secret_masks_plaintext(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "c.toml", SP_OK + BTC_OK)
        cfg = load_config(path)
        assert cfg.simpleproof is not None
        assert cfg.bitcoin is not None
        assert "tok" not in str(cfg.simpleproof.access_token)
        assert "key" not in str(cfg.simpleproof.api_key)
        assert "p" not in str(cfg.bitcoin.rpc_password)


class TestConfigErrorNotValueError:
    def test_config_error_does_not_inherit_value_error(self) -> None:
        assert not issubclass(ConfigError, ValueError), (
            "a stray `except ValueError` would swallow config faults"
        )

    def test_validation_failure_does_not_leak_value_error(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "c.toml",
            SP_OK.replace("https://stage.example", "ftp://bad") + BTC_OK,
        )
        with pytest.raises(ConfigError):
            _load_requiring_ots_sections(path)


class TestBrokenSectionIsDeferred:
    def test_load_records_the_error_and_require_raises_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write(
            tmp_path / "c.toml",
            SP_OK + BTC_OK + "confirmations = 0\n",
        )
        cfg = load_config(path)
        assert cfg.bitcoin is None and cfg.simpleproof is not None
        assert set(cfg.section_errors) == {"bitcoin"}
        cfg.require("simpleproof")
        with pytest.raises(ConfigError, match=r"bitcoin\.confirmations"):
            cfg.require("bitcoin")

    def test_a_broken_toml_still_fails_load(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "c.toml", "[bitcoin\n")
        with pytest.raises(ConfigError):
            load_config(path)


class TestSecretRepr:
    def test_simpleproof_repr_redacts_secrets(self) -> None:
        sp = SimpleProofConfig(
            base_url="https://x.example",
            proof_category_id=1,
            access_token=SecretStr("ACCESS_SECRET_VALUE"),
            api_key=SecretStr("API_SECRET_VALUE"),
        )
        text = repr(sp)
        assert "ACCESS_SECRET_VALUE" not in text
        assert "API_SECRET_VALUE" not in text
        assert "https://x.example" in text, "non-secret fields stay visible"

    def test_bitcoin_repr_redacts_password(self) -> None:
        btc = BitcoinConfig(
            rpc_url="http://127.0.0.1:8332",
            zmq_endpoint="tcp://127.0.0.1:28332",
            rpc_user="u",
            rpc_password=SecretStr("RPC_SECRET_VALUE"),
        )
        text = repr(btc)
        assert "RPC_SECRET_VALUE" not in text
        assert "u" in text, "non-secret fields stay visible"


@pytest.mark.skipif(
    sys.platform == "win32" or os.name == "nt",
    reason="POSIX file modes only",
)
class TestLooseEnvPerms:
    def test_loose_dotenv_perms_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = _write(
            tmp_path / ".env",
            "SIMPLEPROOF_ACCESS_TOKEN=dot-token\n"
            "SIMPLEPROOF_API_KEY=dot-key\n"
            "BITCOIN_RPC_USER=dot-user\n"
            "BITCOIN_RPC_PASSWORD=dot-pass\n",
        )
        os.chmod(env_file, 0o644)
        monkeypatch.setenv("OCFL_ENV_FILE", str(env_file))
        path = _write(tmp_path / "c.toml", SP_OK + BTC_OK)
        with pytest.raises(ConfigError, match=r"chmod 600"):
            _load_requiring_ots_sections(path)

    def test_tight_dotenv_perms_load(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = _write(
            tmp_path / ".env",
            "SIMPLEPROOF_ACCESS_TOKEN=dot-token\n"
            "SIMPLEPROOF_API_KEY=dot-key\n"
            "BITCOIN_RPC_USER=dot-user\n"
            "BITCOIN_RPC_PASSWORD=dot-pass\n",
        )
        os.chmod(env_file, 0o600)
        monkeypatch.setenv("OCFL_ENV_FILE", str(env_file))
        path = _write(tmp_path / "c.toml", SP_OK + BTC_OK)
        assert load_config(path).bitcoin is not None


class TestTomlSecretsRejected:
    @pytest.mark.parametrize(
        ("section", "field", "env_name"),
        [
            ("simpleproof", "access_token", "SIMPLEPROOF_ACCESS_TOKEN"),
            ("simpleproof", "api_key", "SIMPLEPROOF_API_KEY"),
            ("bitcoin", "rpc_user", "BITCOIN_RPC_USER"),
            ("bitcoin", "rpc_password", "BITCOIN_RPC_PASSWORD"),
        ],
    )
    def test_forbidden_keys_name_env_var(
        self, section: str, field: str, env_name: str, tmp_path: Path
    ) -> None:
        extra = f'{field} = "secret-in-toml"\n'
        if section == "simpleproof":
            body = SP_OK + extra + BTC_OK
        else:
            body = SP_OK + BTC_OK + extra
        path = _write(tmp_path / "c.toml", body)
        with pytest.raises(ConfigError, match=env_name):
            _load_requiring_ots_sections(path)


class TestSectionAbsent:
    def test_simpleproof_absent_is_none(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "c.toml", BTC_OK)
        cfg = load_config(path)
        assert cfg.simpleproof is None
        assert cfg.bitcoin is not None

    def test_bitcoin_absent_is_none(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "c.toml", SP_OK)
        cfg = load_config(path)
        assert cfg.bitcoin is None
        assert cfg.simpleproof is not None

    def test_both_absent_both_none(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "c.toml", "")
        cfg = load_config(path)
        assert cfg.simpleproof is None
        assert cfg.bitcoin is None

    def test_no_file_both_none(self, tmp_path: Path) -> None:
        cfg = load_config(tmp_path / "missing.toml")
        assert cfg.simpleproof is None
        assert cfg.bitcoin is None
