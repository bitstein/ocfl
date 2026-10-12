"""ocfl validate command."""

import hashlib
import json
from typing import TYPE_CHECKING, Annotated, NoReturn, cast

import typer

from ocfl_cli import _appctx as appctx
from ocfl_cli import _style
from ocfl_cli._common import make_fs, open_root, output_json, resolve_root
from ocfl_cli._exit import exit_for_error
from ocfl_cli._io import err
from ocfl_cli.errors import (
    AttestationMismatchError,
    BitcoinError,
    ConfigError,
    ReorgDetectedError,
)
from ocfl_cli.ots.bitcoin import (
    PendingReason,
    PendingVerification,
    VerifiedAttestation,
    verify_attestation,
)
from ocfl_lib import (
    OcflError,
    StorageRoot,
    ValidationResult,
    validate_object,
    validate_root,
)
from ocfl_lib.errors import OtsParseError
from ocfl_lib.fs import utils as fs_utils
from ocfl_lib.validation.result import ValidationError

if TYPE_CHECKING:
    from ocfl_cli._appctx import BtcRpcFactory
    from ocfl_cli.config import Config
    from ocfl_lib.fs.protocols import FS, FileWalker


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
    as_json = appctx.json_mode(ctx)
    check_digests = not no_check_digests
    suppress = _suppressed_codes(ctx)

    try:
        if object_id is not None:
            _validate_object(
                ctx, object_id, check_digests, as_json, suppress, verify_ots
            )
        else:
            _validate_root(ctx, check_digests, as_json, suppress)
    except ConfigError as e:
        exit_for_error(e)


def _suppressed_codes(ctx: typer.Context) -> frozenset[str]:
    config = appctx.config(ctx)
    return config.effective_suppress(appctx.archive(ctx))


def _validate_root(
    ctx: typer.Context,
    check_digests: bool,
    json_mode: bool,
    suppress: frozenset[str],
) -> None:
    path = resolve_root(appctx.root(ctx))
    emit_progress = _should_emit_progress(ctx)
    _emit(ctx, f"Validating OCFL storage root: {path}")
    _emit(ctx, "Checking root structure (NAMASTE, layout, extensions)...")
    _emit(ctx, "Walking object hierarchy...")
    try:
        fs = make_fs(path)
    except (OcflError, OSError) as e:
        _fail(str(e), json_mode)

    object_ids = _object_ids_by_path(fs) if emit_progress else {}
    if emit_progress:
        _style.echo(_style.dim(f"  Found {len(object_ids)} objects"), err=True)
    digest_status = "content digests enabled" if check_digests else "digests skipped"
    _emit(ctx, f"Validating objects ({digest_status})...")

    result = ValidationResult()
    summary = validate_root(fs, "", result, check_digests=check_digests)
    _emit_root_object_results(
        emit_progress,
        fs,
        object_ids,
        summary.object_errors,
        truncated=summary.truncated,
    )
    _merge_object_results(result, summary.object_errors)
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
        root, fs = open_root(appctx.root(ctx))
    except typer.Exit:
        raise typer.Exit(1) from None

    try:
        obj_path = root.object_path(object_id)
    except OcflError as e:
        _fail(str(e), json_mode)

    _emit(ctx, f"Validating object: {object_id}")
    _emit(ctx, "Checking OCFL structure...")
    if check_digests:
        _emit(ctx, "Verifying content digests...")

    result = ValidationResult()
    validate_object(fs, obj_path, result, check_digests=check_digests)

    if verify_ots and _ots_extension_present(fs, obj_path):
        cfg = appctx.config(ctx)
        _verify_ots_walk(
            fs,
            obj_path,
            cfg,
            result,
            appctx.btc_rpc_factory(ctx),
            emit_progress=_should_emit_progress(ctx),
        )

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
    btc_factory: BtcRpcFactory,
    *,
    emit_progress: bool = False,
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

    rpc = btc_factory(cfg)
    min_confs = cfg.bitcoin.confirmations
    ext_root = _join(obj_path, "extensions", _OTS_EXT_NAME)
    if emit_progress:
        version_word = "version" if len(versions) == 1 else "versions"
        _style.echo(
            _style.dim(
                "Verifying OTS attestations "
                f"({len(versions)} {version_word}, min {min_confs} confirmations)..."
            ),
            err=True,
        )

    for vkey in versions:
        ots_rel = f"inventory.json.{vkey}.ots"
        ots_path_full = _join(ext_root, ots_rel)
        vinv_path = _join(obj_path, vkey, "inventory.json")
        try:
            ots_bytes = fs_utils.read_all(fs, ots_path_full)
            vinv_bytes = fs_utils.read_all(fs, vinv_path)
        except OSError:
            _emit_ots_result(emit_progress, vkey, ots_rel, "skipped (missing files)")
            continue

        expected = hashlib.sha256(vinv_bytes).digest()

        try:
            outcome = verify_attestation(
                rpc=rpc,
                ots_bytes=ots_bytes,
                expected_digest=expected,
                min_confirmations=min_confs,
            )
        except (
            AttestationMismatchError,
            BitcoinError,
            ReorgDetectedError,
            OtsParseError,
        ) as exc:
            _emit_ots_result(emit_progress, vkey, ots_rel, f"failed ({exc})")
            result.error("E906", path=ots_rel, reason=str(exc))
            continue

        match outcome:
            case VerifiedAttestation(confirmations=confirmations, block_height=height):
                _emit_ots_result(
                    emit_progress,
                    vkey,
                    ots_rel,
                    f"confirmed ({confirmations} confs, block {height})",
                )
                continue
            case PendingVerification(reason=PendingReason.PENDING_ATTESTATION):
                _emit_ots_result(
                    emit_progress, vkey, ots_rel, "pending (pending_attestation)"
                )
                result.warning(
                    "W903", path=ots_rel, reason=PendingReason.PENDING_ATTESTATION.value
                )
                continue
            case PendingVerification(reason=reason):
                _emit_ots_result(
                    emit_progress, vkey, ots_rel, f"pending ({reason.value})"
                )
                result.warning("W903", path=ots_rel, reason=reason.value)


def _enumerate_versions(fs: FS, obj_path: str) -> list[str]:
    """Read inventory.json and return the sorted list of version keys."""
    try:
        inv_bytes = fs_utils.read_all(fs, _join(obj_path, "inventory.json"))
    except OSError:
        return []
    try:
        inv_data = json.loads(inv_bytes)
    except ValueError:
        return []
    if not isinstance(inv_data, dict):
        return []
    versions = inv_data.get("versions")
    if not isinstance(versions, dict):
        return []
    return sorted(k for k in versions if isinstance(k, str))


def _fail(msg: str, json_mode: bool) -> NoReturn:
    """Report an early failure and exit."""
    if json_mode:
        output_json({"valid": False, "errors": [msg], "warnings": []})
    else:
        err(msg)
    raise typer.Exit(1)


def _should_emit_progress(ctx: typer.Context) -> bool:
    if appctx.json_mode(ctx):
        return False
    typed = appctx.app_ctx(ctx)
    return not typed.quiet


def _emit(ctx: typer.Context, msg: str) -> None:
    if _should_emit_progress(ctx):
        _style.echo(_style.dim(msg), err=True)


def _object_ids_by_path(fs: FS) -> dict[str, str]:
    try:
        root = StorageRoot(cast("FileWalker", fs))
    except Exception:
        return {}

    object_ids: dict[str, str] = {}
    for obj_path in root.object_paths():
        object_ids[obj_path] = _read_object_id(fs, obj_path)
    return object_ids


def _read_object_id(fs: FS, obj_path: str) -> str:
    try:
        inv_data = json.loads(fs_utils.read_all(fs, _join(obj_path, "inventory.json")))
    except OSError:
        return obj_path
    except ValueError:
        return obj_path
    if isinstance(inv_data, dict) and isinstance(inv_data.get("id"), str):
        return inv_data["id"]
    return obj_path


def _emit_root_object_results(
    emit_progress: bool,
    fs: FS,
    object_ids: dict[str, str],
    object_errors: list[tuple[str, ValidationResult]],
    *,
    truncated: bool = False,
) -> None:
    if not emit_progress:
        return
    errors_by_path = {obj_path: obj_result for obj_path, obj_result in object_errors}
    for obj_path, object_id in object_ids.items():
        obj_result = errors_by_path.get(obj_path)
        if obj_result is None:
            if truncated:
                _style.echo(
                    f"  {object_id}: not validated (max errors reached)", err=True
                )
                continue
            _style.echo(f"  {object_id}: ok", err=True)
            continue
        count = len(obj_result.errors)
        suffix = "error" if count == 1 else "errors"
        _style.echo(f"  {object_id}: {count} {suffix}", err=True)
    for obj_path, obj_result in errors_by_path.items():
        if obj_path in object_ids:
            continue
        count = len(obj_result.errors)
        suffix = "error" if count == 1 else "errors"
        _style.echo(f"  {_read_object_id(fs, obj_path)}: {count} {suffix}", err=True)


def _emit_ots_result(
    emit_progress: bool, version: str, ots_rel: str, outcome: str
) -> None:
    if emit_progress:
        _style.echo(f"  {version} ({ots_rel}): {outcome}", err=True)


def _merge_object_results(
    result: ValidationResult, object_errors: list[tuple[str, ValidationResult]]
) -> None:
    """Flatten contained-object validation failures into root validation output."""
    for obj_path, obj_result in object_errors:
        for e in obj_result.errors:
            result.errors.append(
                ValidationError(
                    code=e.code,
                    message=f"{obj_path}: {e.message}",
                    is_warning=False,
                )
            )
        for w in obj_result.warnings:
            result.warnings.append(
                ValidationError(
                    code=w.code,
                    message=f"{obj_path}: {w.message}",
                    is_warning=True,
                )
            )


def _output_result(
    result: ValidationResult, json_mode: bool, suppress: frozenset[str]
) -> None:
    warnings = [w for w in result.warnings if w.code not in suppress]
    infos = [i for i in result.infos if i.code not in suppress]
    if json_mode:
        output_json(
            {
                "valid": result.is_valid,
                "errors": [
                    {"code": e.code, "message": e.message} for e in result.errors
                ],
                "warnings": [{"code": w.code, "message": w.message} for w in warnings],
                "info": [{"code": i.code, "message": i.message} for i in infos],
            }
        )
    else:
        for e in result.errors:
            err(f"[{e.code}] {e.message}")
        for w in warnings:
            _style.echo(_style.warning(f"Warning: [{w.code}] {w.message}"), err=True)
        for i in infos:
            _style.echo(_style.dim(f"Info: [{i.code}] {i.message}"), err=True)
        if result.is_valid:
            _style.echo(
                _style.valid(
                    "Valid" + (f" ({len(warnings)} warnings)" if warnings else "")
                )
            )


def _join(*parts: str) -> str:
    return "/".join(p for p in parts if p)
