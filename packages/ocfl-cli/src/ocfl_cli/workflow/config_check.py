from dataclasses import dataclass
from typing import TYPE_CHECKING

from ocfl_cli.config import check_env_file, env_file_path
from ocfl_cli.errors import (
    ConfigCheckFailedError,
    ConfigError,
    ServiceCheckFailedError,
    SimpleProofAuthError,
    SimpleProofError,
)
from ocfl_cli.ots.bitcoin import probe_zmq_endpoint
from ocfl_lib import BitcoinRpcAuthError, BitcoinRpcError, ErrorClass

if TYPE_CHECKING:
    from ocfl_cli.config import Config
    from ocfl_cli.ports import BtcRpcFactory, SpClientFactory

ZMQ_PROBE_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str
    error_class: ErrorClass | None = None

    def as_dict(self) -> dict[str, object]:
        data: dict[str, object] = {"name": self.name, "ok": self.ok}
        data["detail"] = self.detail
        if self.error_class is not None:
            data["error_class"] = self.error_class.value
        return data


def _ok(name: str, detail: str) -> Check:
    return Check(name, True, detail)


def _fail(name: str, detail: str, error_class: ErrorClass) -> Check:
    return Check(name, False, detail, error_class)


def _env_file() -> Check:
    path = env_file_path()
    try:
        check_env_file()
    except ConfigError as e:
        return _fail("env_file", str(e), ErrorClass.CONFIG)
    return _ok("env_file", str(path) if path is not None else "none (no secrets file)")


def _section_error(cfg: Config, name: str) -> Check | None:
    error = cfg.section_errors.get(name)
    return None if error is None else _fail(name, str(error), ErrorClass.CONFIG)


def _simpleproof(cfg: Config, sp_factory: SpClientFactory) -> Check:
    if (failed := _section_error(cfg, "simpleproof")) is not None:
        return failed
    if cfg.simpleproof is None:
        return _ok("simpleproof", "not configured")
    try:
        sp_factory(cfg).check_access()
    except SimpleProofAuthError as e:
        return _fail("simpleproof", str(e), ErrorClass.CONFIG)
    except SimpleProofError as e:
        return _fail("simpleproof", str(e), ErrorClass.SERVICE)
    return _ok("simpleproof", f"{cfg.simpleproof.base_url}: access token accepted")


def _bitcoin_rpc(cfg: Config, btc_factory: BtcRpcFactory) -> Check:
    if (error := cfg.section_errors.get("bitcoin")) is not None:
        return _fail("bitcoin_rpc", str(error), ErrorClass.CONFIG)
    if cfg.bitcoin is None:
        return _ok("bitcoin_rpc", "not configured")
    try:
        info = btc_factory(cfg).getblockchaininfo()
    except BitcoinRpcAuthError as e:
        return _fail("bitcoin_rpc", str(e), ErrorClass.CONFIG)
    except BitcoinRpcError as e:
        return _fail("bitcoin_rpc", str(e), ErrorClass.SERVICE)
    sync = "syncing" if info.initial_block_download else "in sync"
    return _ok("bitcoin_rpc", f"{cfg.bitcoin.rpc_url}: height {info.height}, {sync}")


def _bitcoin_zmq(cfg: Config) -> Check:
    if "bitcoin" in cfg.section_errors:
        return _fail("bitcoin_zmq", "[bitcoin] did not load", ErrorClass.CONFIG)
    if cfg.bitcoin is None:
        return _ok("bitcoin_zmq", "not configured")
    endpoint = cfg.bitcoin.zmq_endpoint
    try:
        probe_zmq_endpoint(endpoint, timeout_s=ZMQ_PROBE_TIMEOUT_SECONDS)
    except OSError as e:
        return _fail("bitcoin_zmq", f"{endpoint}: {e}", ErrorClass.SERVICE)
    return _ok("bitcoin_zmq", f"{endpoint}: listening")


def broken_config_checks(error: ConfigError) -> tuple[Check, ...]:
    return (_fail("config", str(error), ErrorClass.CONFIG), _env_file())


def run_checks(
    cfg: Config, *, sp_factory: SpClientFactory, btc_factory: BtcRpcFactory
) -> tuple[Check, ...]:
    where = str(cfg.path) if cfg.path is not None else "no config file (defaults)"
    return (
        _ok("config", where),
        _env_file(),
        _simpleproof(cfg, sp_factory),
        _bitcoin_rpc(cfg, btc_factory),
        _bitcoin_zmq(cfg),
    )


def require_all_ok(checks: tuple[Check, ...]) -> None:
    failed = [c for c in checks if not c.ok]
    if not failed:
        return
    names = ", ".join(c.name for c in failed)
    if any(c.error_class is ErrorClass.CONFIG for c in failed):
        raise ConfigCheckFailedError(f"config check failed: {names}")
    raise ServiceCheckFailedError(f"config check failed: {names}")
