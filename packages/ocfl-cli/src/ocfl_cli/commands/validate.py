"""ocfl validate command."""

import base64
import hashlib
import json
from typing import TYPE_CHECKING, Annotated, NoReturn

import httpx
import typer

from ocfl_cli import _style
from ocfl_cli._common import _make_fs, err, open_root, output_json, resolve_root
from ocfl_cli.bitcoin import (
    BitcoinRPC,
    BitcoinRPCClient,
    PendingReason,
    PendingVerification,
    VerifiedAttestation,
    verify_attestation,
)
from ocfl_cli.errors import (
    AttestationMismatchError,
    ConfigError,
    ReorgDetectedError,
)
from ocfl_lib import OcflError, ValidationResult, validate_object, validate_root
from ocfl_lib.fs import utils as fs_utils

if TYPE_CHECKING:
    from ocfl_cli.config import Config
    from ocfl_lib.fs.protocols import FS


_OTS_EXT_NAME = "NNNN-ots-timestamps"


def validate(
    ctx: typer.Context,
    object_id: Annotated[
        str | None, typer.Argument(help="Object ID to validate.")
    ] = None,
    no_check_digests: Annotated[
        bool,
        typer.Option("--no-check-digests", help="Skip content digest verification."),
    ] = False,
    verify_ots: Annotated[
        bool,
        typer.Option(
            "--verify-ots/--no-verify-ots",
            help=(
                "Walk OTS attestation chain to Bitcoin when the "
                "NNNN-ots-timestamps extension is present (default on)."
            ),
        ),
    ] = True,
) -> None:
    """Validate an OCFL storage root or a specific object."""
    json_mode = ctx.obj.get("json", False)
    check_digests = not no_check_digests
    suppress = _suppressed_codes(ctx)

    try:
        if object_id is not None:
            _validate_object(
                ctx, object_id, check_digests, json_mode, suppress, verify_ots
            )
        else:
            _validate_root(ctx, check_digests, json_mode, suppress)
    except ConfigError as e:
        err(str(e))
        raise typer.Exit(2) from None


def _suppressed_codes(ctx: typer.Context) -> frozenset[str]:
    config = ctx.obj.get("config")
    if config is None:
        return frozenset()
    return config.effective_suppress(ctx.obj.get("archive"))


def _validate_root(
    ctx: typer.Context,
    check_digests: bool,
    json_mode: bool,
    suppress: frozenset[str],
) -> None:
    path = resolve_root(ctx.obj.get("root"))
    try:
        fs = _make_fs(path)
    except (OcflError, OSError) as e:
        _fail(str(e), json_mode)

    result = ValidationResult()
    validate_root(fs, "", result, check_digests=check_digests)
    _output_result(result, json_mode, suppress)
    if not result.is_valid:
        raise typer.Exit(1)


def _validate_object(
    ctx: typer.Context,
    object_id: str,
    check_digests: bool,
    json_mode: bool,
    suppress: frozenset[str],
    verify_ots: bool,
) -> None:
    try:
        root, fs = open_root(ctx.obj.get("root"))
    except typer.Exit:
        raise typer.Exit(1) from None

    try:
        obj_path = root.object_path(object_id)
    except OcflError as e:
        _fail(str(e), json_mode)

    result = ValidationResult()
    validate_object(fs, obj_path, result, check_digests=check_digests)

    if verify_ots and _ots_extension_present(fs, obj_path):
        cfg = ctx.obj.get("config")
        _verify_ots_walk(fs, obj_path, cfg, result)

    _output_result(result, json_mode, suppress)
    if not result.is_valid:
        raise typer.Exit(1)


def _ots_extension_present(fs: FS, obj_path: str) -> bool:
    cfg_path = _join(obj_path, "extensions", _OTS_EXT_NAME, "config.json")
    return fs.exists(cfg_path)


def _verify_ots_walk(
    fs: FS,
    obj_path: str,
    cfg: Config | None,
    result: ValidationResult,
) -> None:
    """Walk extension `inventory.json.{vkey}.ots` files through ``verify_attestation``.

    The per-version inventory attestation anchors the inventory bytes to
    Bitcoin; verifying it transitively covers the manifest digests of all
    content files in that version. Scope is single-object validate of the
    inventory chain; content `.ots` companions and root-level enumeration
    are deferred.
    """
    if cfg is None or cfg.bitcoin is None:
        where = (
            f"in {cfg.path}"
            if cfg is not None and cfg.path is not None
            else "in your ocfl config"
        )
        raise ConfigError(f"--verify-ots requires [bitcoin] section {where}")

    versions = _enumerate_versions(fs, obj_path)
    if not versions:
        return

    rpc = _get_btc_rpc(cfg)
    min_confs = cfg.bitcoin.confirmations
    ext_root = _join(obj_path, "extensions", _OTS_EXT_NAME)

    for vkey in versions:
        ots_rel = f"inventory.json.{vkey}.ots"
        ots_path_full = _join(ext_root, ots_rel)
        vinv_path = _join(obj_path, vkey, "inventory.json")
        try:
            ots_bytes = fs_utils.read_all(fs, ots_path_full)
            vinv_bytes = fs_utils.read_all(fs, vinv_path)
        except OSError:
            continue

        expected = hashlib.sha256(vinv_bytes).digest()

        try:
            outcome = verify_attestation(
                rpc=rpc,
                ots_bytes=ots_bytes,
                expected_digest=expected,
                min_confirmations=min_confs,
            )
        except (AttestationMismatchError, ReorgDetectedError) as exc:
            result.error("E906", path=ots_rel, reason=str(exc))
            continue

        match outcome:
            case VerifiedAttestation():
                continue
            case PendingVerification(reason=PendingReason.PENDING_ATTESTATION):
                # Already covered by W902 from structural validation.
                continue
            case PendingVerification(reason=reason):
                result.warning("W903", path=ots_rel, reason=reason.value)


def _enumerate_versions(fs: FS, obj_path: str) -> list[str]:
    """Read inventory.json and return the sorted list of version keys."""
    try:
        inv_bytes = fs_utils.read_all(fs, _join(obj_path, "inventory.json"))
    except OSError:
        return []
    try:
        inv_data = json.loads(inv_bytes)
    except json.JSONDecodeError, ValueError:
        return []
    if not isinstance(inv_data, dict):
        return []
    versions = inv_data.get("versions")
    if not isinstance(versions, dict):
        return []
    return sorted(k for k in versions if isinstance(k, str))


def _get_btc_rpc(cfg: Config) -> BitcoinRPC:
    """Construct the production `BitcoinRPCClient` from `cfg`.

    Tests monkeypatch this seam to inject `FakeBitcoinRPC`.
    """
    btc_cfg = cfg.bitcoin
    if btc_cfg is None:
        raise RuntimeError("bitcoin config missing")

    creds = f"{btc_cfg.rpc_user}:{btc_cfg.rpc_password.get_secret_value()}"
    headers = {"Authorization": "Basic " + base64.b64encode(creds.encode()).decode()}
    client = httpx.Client(timeout=btc_cfg.rpc_timeout_s, headers=headers)
    return BitcoinRPCClient(btc_cfg.rpc_url, client=client)


def _fail(msg: str, json_mode: bool) -> NoReturn:
    """Report an early failure and exit."""
    if json_mode:
        output_json({"valid": False, "errors": [msg], "warnings": []})
    else:
        err(msg)
    raise typer.Exit(1)


def _output_result(
    result: ValidationResult, json_mode: bool, suppress: frozenset[str]
) -> None:
    warnings = [w for w in result.warnings if w.code not in suppress]
    if json_mode:
        output_json(
            {
                "valid": result.is_valid,
                "errors": [
                    {"code": e.code, "message": e.message} for e in result.errors
                ],
                "warnings": [{"code": w.code, "message": w.message} for w in warnings],
            }
        )
    else:
        for e in result.errors:
            err(f"[{e.code}] {e.message}")
        for w in warnings:
            _style.echo(_style.warning(f"Warning: [{w.code}] {w.message}"), err=True)
        if result.is_valid:
            _style.echo(
                _style.valid(
                    "Valid" + (f" ({len(warnings)} warnings)" if warnings else "")
                )
            )


def _join(*parts: str) -> str:
    return "/".join(p for p in parts if p)
