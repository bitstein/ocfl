"""Typed preflight helpers for stage commands."""

import click
import pytest
import typer
from pydantic import SecretStr

from ocfl_cli.config import BitcoinConfig, Config, SimpleProofConfig
from ocfl_cli.exit_codes import ExitCode
from ocfl_cli.stage.commands import (
    StagingResolved,
    TimestampPreflight,
    get_factories,
    require_bitcoin,
    require_existing_staging,
    require_ots_staging,
    require_simpleproof,
    require_timestamp_preflight,
    validate_timestamp_flags,
)
from ocfl_cli.stage.fsm import Phase
from ocfl_cli.stage.state import OtsState, save_ots
from ocfl_cli.stage.store import StagingStore
from ocfl_lib import DigestAlgorithm
from ocfl_lib.inventory import Inventory
from ocfl_lib.inventory_builder import InventoryBuilder


def _ctx(
    obj: dict[str, object], *, command_parts: tuple[str, ...] = ("probe",)
) -> typer.Context:
    ctx: typer.Context | None = typer.Context(
        click.Command("ocfl"), info_name="ocfl", obj=obj
    )
    for part in command_parts:
        ctx = typer.Context(click.Command(part), info_name=part, parent=ctx, obj=obj)
    return ctx


def _inventory(object_id: str = "obj:test") -> Inventory:
    return (
        InventoryBuilder()
        .id(object_id)
        .digest_algorithm(DigestAlgorithm.SHA512)
        .add_version({}, created="1970-01-01T00:00:00Z")
        .build()
    )


@pytest.mark.parametrize(
    ("upgrade", "watch", "retry", "expected"),
    [
        (True, False, True, "--upgrade"),
        (False, True, True, "--watch"),
    ],
)
def test_validate_timestamp_flags_rejects_retry_combinations(
    upgrade: bool, watch: bool, retry: bool, expected: str, capsys
) -> None:
    with pytest.raises(typer.Exit) as exc_info:
        validate_timestamp_flags(
            _ctx({}, command_parts=("stage", "timestamp", "content")),
            upgrade=upgrade,
            watch=watch,
            retry=retry,
        )

    captured = capsys.readouterr()
    assert exc_info.value.exit_code == int(ExitCode.USAGE)
    assert expected in captured.err


def test_validate_timestamp_flags_rejects_watch_without_upgrade(capsys) -> None:
    with pytest.raises(typer.Exit) as exc_info:
        validate_timestamp_flags(
            _ctx({}, command_parts=("stage", "timestamp", "inventory")),
            upgrade=False,
            watch=True,
            retry=False,
        )

    captured = capsys.readouterr()
    assert exc_info.value.exit_code == int(ExitCode.USAGE)
    assert "requires --upgrade" in captured.err


def test_require_simpleproof_rejects_missing_section(capsys) -> None:
    ctx = _ctx({}, command_parts=("stage", "timestamp", "content"))

    with pytest.raises(typer.Exit) as exc_info:
        require_simpleproof(ctx, Config())

    captured = capsys.readouterr()
    assert exc_info.value.exit_code == int(ExitCode.CONFIG)
    assert "'stage timestamp content' requires [simpleproof]" in captured.err


def test_require_bitcoin_rejects_missing_section(capsys) -> None:
    ctx = _ctx({}, command_parts=("stage", "timestamp", "content"))

    with pytest.raises(typer.Exit) as exc_info:
        require_bitcoin(ctx, Config())

    captured = capsys.readouterr()
    assert exc_info.value.exit_code == int(ExitCode.CONFIG)
    assert "'stage timestamp content' requires [bitcoin]" in captured.err


def test_require_existing_staging_returns_typed_resolution(tmp_path) -> None:
    store = StagingStore(tmp_path / "staging")
    root_path = str((tmp_path / "root").resolve())
    store.create(root_path, "obj:test", _inventory())
    ctx = _ctx({"staging": store, "root": str(tmp_path / "root")})

    resolved = require_existing_staging(ctx, "obj:test")

    assert isinstance(resolved, StagingResolved)
    assert resolved.store is store
    assert resolved.root_path == root_path
    assert resolved.staging_dir == store.staging_path(root_path, "obj:test")


def test_require_existing_staging_includes_optional_hint(tmp_path, capsys) -> None:
    store = StagingStore(tmp_path / "staging")
    ctx = _ctx({"staging": store, "root": str(tmp_path / "root")})

    with pytest.raises(typer.Exit) as exc_info:
        require_existing_staging(ctx, "obj:missing", hint="run 'stage new' first")

    captured = capsys.readouterr()
    assert exc_info.value.exit_code == int(ExitCode.GENERIC)
    assert "run 'stage new' first" in captured.err


def test_require_ots_staging_rejects_non_ots_staging(tmp_path, capsys) -> None:
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()

    with pytest.raises(typer.Exit) as exc_info:
        require_ots_staging(staging_dir, "obj:test")

    captured = capsys.readouterr()
    assert exc_info.value.exit_code == int(ExitCode.GENERIC)
    assert "is not an OTS staging" in captured.err


def test_require_ots_staging_accepts_ots_state(tmp_path) -> None:
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    save_ots(staging_dir, OtsState(phase=Phase.DRAFTING, category_id=641))

    require_ots_staging(staging_dir, "obj:test")


def test_get_factories_returns_injected_factories() -> None:
    def sp_factory(_cfg):
        return object()

    def btc_factory(_cfg):
        return object()

    ctx = _ctx({"sp_client_factory": sp_factory, "btc_rpc_factory": btc_factory})

    assert get_factories(ctx) == (sp_factory, btc_factory)


def test_require_timestamp_preflight_returns_validated_context(tmp_path) -> None:
    store = StagingStore(tmp_path / "staging")
    root_path = str((tmp_path / "root").resolve())
    store.create(root_path, "obj:test", _inventory())
    staging_dir = store.staging_path(root_path, "obj:test")
    save_ots(staging_dir, OtsState(phase=Phase.DRAFTING, category_id=641))

    def sp_factory(_cfg):
        return object()

    def btc_factory(_cfg):
        return object()

    cfg = Config(
        simpleproof=SimpleProofConfig(
            base_url="https://simpleproof.example",
            proof_category_id=641,
        ),
        bitcoin=BitcoinConfig(
            rpc_url="http://bitcoin.example",
            zmq_endpoint="tcp://127.0.0.1:28332",
            rpc_user="user",
            rpc_password=SecretStr("pass"),
        ),
    )
    ctx = _ctx(
        {
            "config": cfg,
            "root": str(tmp_path / "root"),
            "staging": store,
            "sp_client_factory": sp_factory,
            "btc_rpc_factory": btc_factory,
        },
        command_parts=("stage", "timestamp", "content"),
    )

    preflight = require_timestamp_preflight(
        ctx,
        "obj:test",
        upgrade=True,
        watch=False,
        retry=False,
    )

    assert isinstance(preflight, TimestampPreflight)
    assert preflight.cfg is cfg
    assert preflight.resolved.staging_dir == staging_dir
    assert preflight.sp_factory is sp_factory
    assert preflight.btc_factory is btc_factory
