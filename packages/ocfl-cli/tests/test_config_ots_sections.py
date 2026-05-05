"""Tests for [simpleproof] + [bitcoin] config sections.

Covers the SimpleProofConfig + BitcoinConfig pydantic settings models: URL
schemes, rpc_user/rpc_password requirements, confirmations range, env override
precedence, secret repr redaction, and loose-perms warning. Locks in the API
exposed by :mod:`ocfl_cli.config`.

Decision: WARNING assertions go through ``caplog`` (Python logging), not
``capsys``. ``main._configure_logging`` already routes the root logger to
stderr via ``StreamHandler(sys.stderr)`` — capturing logs is cleaner and
avoids coupling tests to formatter output.
"""

import logging
import os
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from ocfl_cli.config import (
    BitcoinConfig,
    Config,
    ConfigError,
    SimpleProofConfig,
    load_config,
)


def test_ots_config_classes_reexported_from_domain_module() -> None:
    from ocfl_cli.ots.config import BitcoinConfig as DomainBitcoinConfig
    from ocfl_cli.ots.config import SimpleProofConfig as DomainSimpleProofConfig

    assert SimpleProofConfig is DomainSimpleProofConfig
    assert BitcoinConfig is DomainBitcoinConfig


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


# Reusable section snippets. Tests compose these or override individual
# lines via ``.replace()`` to keep TOML inline and obvious.

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
"""


# --------------------------------------------------------------------------- #
# 1. Valid load
# --------------------------------------------------------------------------- #


class TestValidLoad:
    def test_both_sections_populated(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "c.toml", _SP_OK + _BTC_OK)
        cfg = load_config(path)
        assert isinstance(cfg, Config)
        assert isinstance(cfg.simpleproof, SimpleProofConfig)
        assert cfg.simpleproof.base_url == "https://stage.example"
        assert cfg.simpleproof.proof_category_id == 641
        assert cfg.simpleproof.access_token.get_secret_value() == "tok"
        assert cfg.simpleproof.api_key.get_secret_value() == "key"
        assert cfg.simpleproof.timeout_s == 30  # default

        assert isinstance(cfg.bitcoin, BitcoinConfig)
        assert cfg.bitcoin.rpc_url == "http://127.0.0.1:8332"
        assert cfg.bitcoin.zmq_endpoint == "tcp://127.0.0.1:28332"
        assert cfg.bitcoin.rpc_user == "u"
        assert cfg.bitcoin.rpc_password.get_secret_value() == "p"
        assert cfg.bitcoin.confirmations == 3  # default
        assert cfg.bitcoin.rpc_timeout_s == 10  # default

    def test_explicit_overrides(self, tmp_path: Path) -> None:
        body = (
            _SP_OK.replace("proof_category_id = 641", "proof_category_id = 99")
            + "timeout_s = 60\n"
            + _BTC_OK
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


# --------------------------------------------------------------------------- #
# 2-4. URL scheme checks
# --------------------------------------------------------------------------- #


class TestUrlSchemes:
    @pytest.mark.parametrize(
        "scheme",
        ["http://stage.example", "https://stage.example"],
    )
    def test_simpleproof_base_url_ok(self, scheme: str, tmp_path: Path) -> None:
        body = _SP_OK.replace("https://stage.example", scheme) + _BTC_OK
        path = _write(tmp_path / "c.toml", body)
        cfg = load_config(path)
        assert cfg.simpleproof is not None
        assert cfg.simpleproof.base_url == scheme

    @pytest.mark.parametrize(
        "bad_url",
        ["ftp://stage.example", "tcp://x", "file:///x", "stage.example"],
    )
    def test_simpleproof_base_url_rejects_non_http(
        self, bad_url: str, tmp_path: Path
    ) -> None:
        body = _SP_OK.replace("https://stage.example", bad_url) + _BTC_OK
        path = _write(tmp_path / "c.toml", body)
        with pytest.raises(ConfigError):
            load_config(path)

    @pytest.mark.parametrize(
        "scheme",
        ["http://127.0.0.1:8332", "https://node.example:8332"],
    )
    def test_bitcoin_rpc_url_ok(self, scheme: str, tmp_path: Path) -> None:
        body = _SP_OK + _BTC_OK.replace("http://127.0.0.1:8332", scheme)
        path = _write(tmp_path / "c.toml", body)
        cfg = load_config(path)
        assert cfg.bitcoin is not None
        assert cfg.bitcoin.rpc_url == scheme

    @pytest.mark.parametrize(
        "bad_url",
        ["ftp://x", "tcp://127.0.0.1:8332", "127.0.0.1:8332"],
    )
    def test_bitcoin_rpc_url_rejects_non_http(
        self, bad_url: str, tmp_path: Path
    ) -> None:
        body = _SP_OK + _BTC_OK.replace("http://127.0.0.1:8332", bad_url)
        path = _write(tmp_path / "c.toml", body)
        with pytest.raises(ConfigError):
            load_config(path)

    @pytest.mark.parametrize(
        "endpoint",
        ["tcp://127.0.0.1:28332", "ipc:///tmp/zmq.sock"],
    )
    def test_zmq_endpoint_ok(self, endpoint: str, tmp_path: Path) -> None:
        body = _SP_OK + _BTC_OK.replace("tcp://127.0.0.1:28332", endpoint)
        path = _write(tmp_path / "c.toml", body)
        cfg = load_config(path)
        assert cfg.bitcoin is not None
        assert cfg.bitcoin.zmq_endpoint == endpoint

    @pytest.mark.parametrize(
        "bad",
        ["http://127.0.0.1:28332", "https://x", "127.0.0.1:28332"],
    )
    def test_zmq_endpoint_rejects_non_tcp_ipc(self, bad: str, tmp_path: Path) -> None:
        body = _SP_OK + _BTC_OK.replace("tcp://127.0.0.1:28332", bad)
        path = _write(tmp_path / "c.toml", body)
        with pytest.raises(ConfigError):
            load_config(path)


# --------------------------------------------------------------------------- #
# 5. rpc_user + rpc_password required
# --------------------------------------------------------------------------- #


class TestRpcAuth:
    def test_user_pass_ok(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "c.toml", _SP_OK + _BTC_OK)
        cfg = load_config(path)
        assert cfg.bitcoin is not None
        assert cfg.bitcoin.rpc_user == "u"
        assert cfg.bitcoin.rpc_password.get_secret_value() == "p"

    def test_missing_user_rejected(self, tmp_path: Path) -> None:
        body = _SP_OK + (
            "[bitcoin]\n"
            'rpc_url = "http://127.0.0.1:8332"\n'
            'zmq_endpoint = "tcp://127.0.0.1:28332"\n'
            'rpc_password = "p"\n'
        )
        path = _write(tmp_path / "c.toml", body)
        with pytest.raises(ConfigError):
            load_config(path)

    def test_missing_password_rejected(self, tmp_path: Path) -> None:
        body = _SP_OK + (
            "[bitcoin]\n"
            'rpc_url = "http://127.0.0.1:8332"\n'
            'zmq_endpoint = "tcp://127.0.0.1:28332"\n'
            'rpc_user = "u"\n'
        )
        path = _write(tmp_path / "c.toml", body)
        with pytest.raises(ConfigError):
            load_config(path)

    def test_missing_both_rejected(self, tmp_path: Path) -> None:
        body = _SP_OK + (
            "[bitcoin]\n"
            'rpc_url = "http://127.0.0.1:8332"\n'
            'zmq_endpoint = "tcp://127.0.0.1:28332"\n'
        )
        path = _write(tmp_path / "c.toml", body)
        with pytest.raises(ConfigError):
            load_config(path)


# --------------------------------------------------------------------------- #
# 6. confirmations range + WARNING for <3
# --------------------------------------------------------------------------- #


def _btc_with_conf(n: int) -> str:
    return _BTC_OK + f"confirmations = {n}\n"


class TestConfirmationsRange:
    @pytest.mark.parametrize("n", [0, -1, 101, 1000])
    def test_out_of_range_errors(self, n: int, tmp_path: Path) -> None:
        path = _write(tmp_path / "c.toml", _SP_OK + _btc_with_conf(n))
        with pytest.raises(ConfigError):
            load_config(path)

    @pytest.mark.parametrize("n", [1, 2])
    def test_below_three_warns(
        self, n: int, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = _write(tmp_path / "c.toml", _SP_OK + _btc_with_conf(n))
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
        path = _write(tmp_path / "c.toml", _SP_OK + _btc_with_conf(n))
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


# --------------------------------------------------------------------------- #
# 7. Env override precedence (env > toml > default)
# --------------------------------------------------------------------------- #


class TestEnvOverride:
    def test_base_url_env_wins_over_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SIMPLEPROOF_BASE_URL", "https://prod.example")
        path = _write(tmp_path / "c.toml", _SP_OK + _BTC_OK)
        cfg = load_config(path)
        assert cfg.simpleproof is not None
        assert cfg.simpleproof.base_url == "https://prod.example"

    def test_access_token_env_wins_over_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SIMPLEPROOF_ACCESS_TOKEN", "ENVTOKEN")
        path = _write(tmp_path / "c.toml", _SP_OK + _BTC_OK)
        cfg = load_config(path)
        assert cfg.simpleproof is not None
        assert cfg.simpleproof.access_token.get_secret_value() == "ENVTOKEN"

    def test_api_key_env_wins_over_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SIMPLEPROOF_API_KEY", "ENVKEY")
        path = _write(tmp_path / "c.toml", _SP_OK + _BTC_OK)
        cfg = load_config(path)
        assert cfg.simpleproof is not None
        assert cfg.simpleproof.api_key.get_secret_value() == "ENVKEY"

    def test_env_validates_scheme_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Env override participates in validation — bad scheme in env still fails."""
        monkeypatch.setenv("SIMPLEPROOF_BASE_URL", "ftp://bad")
        path = _write(tmp_path / "c.toml", _SP_OK + _BTC_OK)
        with pytest.raises(ConfigError):
            load_config(path)


# --------------------------------------------------------------------------- #
# 7b. Bitcoin env override (BITCOIN_* mirrors SIMPLEPROOF_*)
# --------------------------------------------------------------------------- #


class TestBitcoinEnvOverride:
    def test_rpc_url_env_wins_over_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BITCOIN_RPC_URL", "http://10.0.0.1:8332")
        path = _write(tmp_path / "c.toml", _SP_OK + _BTC_OK)
        cfg = load_config(path)
        assert cfg.bitcoin is not None
        assert cfg.bitcoin.rpc_url == "http://10.0.0.1:8332"

    def test_rpc_user_env_wins_over_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BITCOIN_RPC_USER", "envuser")
        path = _write(tmp_path / "c.toml", _SP_OK + _BTC_OK)
        cfg = load_config(path)
        assert cfg.bitcoin is not None
        assert cfg.bitcoin.rpc_user == "envuser"

    def test_rpc_password_env_wins_over_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BITCOIN_RPC_PASSWORD", "ENVPASS")
        path = _write(tmp_path / "c.toml", _SP_OK + _BTC_OK)
        cfg = load_config(path)
        assert cfg.bitcoin is not None
        assert cfg.bitcoin.rpc_password.get_secret_value() == "ENVPASS"

    def test_zmq_endpoint_env_wins_over_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BITCOIN_ZMQ_ENDPOINT", "tcp://10.0.0.1:28332")
        path = _write(tmp_path / "c.toml", _SP_OK + _BTC_OK)
        cfg = load_config(path)
        assert cfg.bitcoin is not None
        assert cfg.bitcoin.zmq_endpoint == "tcp://10.0.0.1:28332"

    def test_env_validates_scheme_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Env override participates in validation — bad scheme in env still fails."""
        monkeypatch.setenv("BITCOIN_RPC_URL", "ftp://bad")
        path = _write(tmp_path / "c.toml", _SP_OK + _BTC_OK)
        with pytest.raises(ConfigError):
            load_config(path)


# --------------------------------------------------------------------------- #
# 7c. SecretStr API: tokens/passwords are SecretStr, not str
# --------------------------------------------------------------------------- #


class TestSecretStrAPI:
    def test_simpleproof_secrets_are_secret_str(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "c.toml", _SP_OK + _BTC_OK)
        cfg = load_config(path)
        assert cfg.simpleproof is not None
        assert isinstance(cfg.simpleproof.access_token, SecretStr)
        assert isinstance(cfg.simpleproof.api_key, SecretStr)
        assert cfg.simpleproof.access_token.get_secret_value() == "tok"
        assert cfg.simpleproof.api_key.get_secret_value() == "key"

    def test_bitcoin_password_is_secret_str(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "c.toml", _SP_OK + _BTC_OK)
        cfg = load_config(path)
        assert cfg.bitcoin is not None
        assert isinstance(cfg.bitcoin.rpc_password, SecretStr)
        assert cfg.bitcoin.rpc_password.get_secret_value() == "p"

    def test_str_of_secret_masks_plaintext(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "c.toml", _SP_OK + _BTC_OK)
        cfg = load_config(path)
        assert cfg.simpleproof is not None
        assert cfg.bitcoin is not None
        assert "tok" not in str(cfg.simpleproof.access_token)
        assert "key" not in str(cfg.simpleproof.api_key)
        assert "p" not in str(cfg.bitcoin.rpc_password)


# --------------------------------------------------------------------------- #
# 8. ConfigError vs ValueError
# --------------------------------------------------------------------------- #


class TestConfigErrorNotValueError:
    def test_config_error_does_not_inherit_value_error(self) -> None:
        """ConfigError must drop the ValueError base. Stray ``except ValueError``
        handlers in unrelated code paths must not silently swallow operator-
        facing config faults."""
        assert not issubclass(ConfigError, ValueError)

    def test_validation_failure_does_not_leak_value_error(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "c.toml",
            _SP_OK.replace("https://stage.example", "ftp://bad") + _BTC_OK,
        )
        try:
            load_config(path)
        except ConfigError:
            pass  # expected
        except ValueError as e:  # pragma: no cover - failure path
            pytest.fail(f"ValueError leaked from load_config: {e!r}")
        else:  # pragma: no cover - failure path
            pytest.fail("expected ConfigError")


# --------------------------------------------------------------------------- #
# 9. Secret repr redaction
# --------------------------------------------------------------------------- #


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
        # Non-secret fields still visible.
        assert "https://x.example" in text

    def test_bitcoin_repr_redacts_password(self) -> None:
        btc = BitcoinConfig(
            rpc_url="http://127.0.0.1:8332",
            zmq_endpoint="tcp://127.0.0.1:28332",
            rpc_user="u",
            rpc_password=SecretStr("RPC_SECRET_VALUE"),
        )
        text = repr(btc)
        assert "RPC_SECRET_VALUE" not in text
        # Non-secret fields still visible.
        assert "u" in text


# --------------------------------------------------------------------------- #
# 10. File-permission warning on loose perms + secret
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    sys.platform == "win32" or os.name == "nt",
    reason="POSIX file modes only",
)
class TestLoosePermsWarning:
    def _warn_records(
        self, caplog: pytest.LogCaptureFixture
    ) -> list[logging.LogRecord]:
        return [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING
            and (
                "permission" in r.message.lower()
                or "chmod" in r.message.lower()
                or "world-readable" in r.message.lower()
                or "group-readable" in r.message.lower()
            )
        ]

    def test_loose_perms_with_secret_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = _write(tmp_path / "c.toml", _SP_OK + _BTC_OK)
        os.chmod(path, 0o644)  # group/world-readable
        with caplog.at_level(logging.WARNING):
            load_config(path)
        records = self._warn_records(caplog)
        assert records, "expected loose-perms WARNING when secret present"

    def test_tight_perms_no_warn(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = _write(tmp_path / "c.toml", _SP_OK + _BTC_OK)
        os.chmod(path, 0o600)
        with caplog.at_level(logging.WARNING):
            load_config(path)
        assert not self._warn_records(caplog)

    def test_loose_perms_without_secret_no_warn(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # No [simpleproof], no [bitcoin] — no secret-bearing section.
        body = '[archives.alpha]\nroot = "/srv/a"\n'
        path = _write(tmp_path / "c.toml", body)
        os.chmod(path, 0o644)
        with caplog.at_level(logging.WARNING):
            load_config(path)
        assert not self._warn_records(caplog)


# --------------------------------------------------------------------------- #
# 11. Section absent → None
# --------------------------------------------------------------------------- #


class TestSectionAbsent:
    def test_simpleproof_absent_is_none(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "c.toml", _BTC_OK)
        cfg = load_config(path)
        assert cfg.simpleproof is None
        assert cfg.bitcoin is not None

    def test_bitcoin_absent_is_none(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "c.toml", _SP_OK)
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
