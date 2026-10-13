"""ocfl validate command."""

import json
from typing import TYPE_CHECKING, Annotated, cast

import typer

from ocfl_cli import _appctx as appctx
from ocfl_cli import _style
from ocfl_cli._common import open_location, open_root, output_json, resolve_root
from ocfl_cli._exit import error_line
from ocfl_cli.errors import ConfigError, ErrorContext
from ocfl_cli.workflow import validation as wf_validation
from ocfl_lib import (
    Finding,
    OcflError,
    StorageRoot,
    ValidationReport,
    ots,
    validate_object,
    validate_root,
)
from ocfl_lib import fs as fs_utils

if TYPE_CHECKING:
    from rich.console import Console

    from ocfl_cli.config import Config
    from ocfl_cli.ports import BtcRpcFactory
    from ocfl_lib.fs import FS, FileWalker


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
                f"{ots.EXTENSION_NAME} extension is present (default on)."
            ),
        ),
    ] = True,
) -> None:
    """Validate an OCFL storage root or a specific object."""
    as_json = appctx.json_mode(ctx)
    check_digests = not no_check_digests
    suppress = _suppressed_codes(ctx)

    if object_id is not None:
        _validate_object(ctx, object_id, check_digests, as_json, suppress, verify_ots)
    else:
        _validate_root(ctx, check_digests, as_json, suppress)


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
    fs = open_location(ctx, path)
    object_ids = _object_ids_by_path(fs) if emit_progress else {}
    if emit_progress:
        appctx.err_console(ctx).print(_style.dim(f"  Found {len(object_ids)} objects"))
    digest_status = "content digests enabled" if check_digests else "digests skipped"
    _emit(ctx, f"Validating objects ({digest_status})...")

    report = validate_root(fs, "", check_digests=check_digests)
    _emit_root_object_results(
        appctx.err_console(ctx), emit_progress, fs, object_ids, report
    )
    _output_result(
        appctx.console(ctx),
        appctx.err_console(ctx),
        report,
        json_mode,
        suppress,
        show_paths=True,
    )
    if not report.is_valid:
        raise typer.Exit(1)


def _validate_object(
    ctx: typer.Context,
    object_id: str,
    check_digests: bool,
    json_mode: bool,
    suppress: frozenset[str],
    verify_ots: bool,
) -> None:
    root, fs = open_root(ctx)
    obj_path = root.object_path(object_id)

    _emit(ctx, f"Validating object: {object_id}")
    _emit(ctx, "Checking OCFL structure...")
    if check_digests:
        _emit(ctx, "Verifying content digests...")

    chain = None
    if verify_ots and _ots_extension_present(fs, obj_path):
        chain = _chain_check(appctx.config(ctx), appctx.btc_rpc_factory(ctx))
        _emit(
            ctx,
            f"Verifying OTS attestations (min {chain.confirmations} confirmations)...",
        )
    try:
        report = validate_object(fs, obj_path, check_digests=check_digests, chain=chain)
        if chain is not None and _should_emit_progress(ctx):
            _emit_chain_results(
                appctx.err_console(ctx), ots.check_object_chain(fs, obj_path, chain)
            )
    except OcflError as e:
        # The chain could not be checked: a failure, not a finding (no E906).
        raise ErrorContext(e, prefix="cannot verify OTS attestations: ") from e

    _output_result(
        appctx.console(ctx),
        appctx.err_console(ctx),
        report,
        json_mode,
        suppress,
        show_paths=False,
    )
    if not report.is_valid:
        raise typer.Exit(1)


def _ots_extension_present(fs: FS, obj_path: str) -> bool:
    return fs.exists(_join(obj_path, ots.EXTENSION_DIR))


def _chain_check(cfg: Config | None, btc_factory: BtcRpcFactory) -> ots.ChainCheck:
    """The Bitcoin checks of the OTS extension, against the configured node."""
    if cfg is None or cfg.bitcoin is None:
        where = (
            f"in {cfg.path}"
            if cfg is not None and cfg.path is not None
            else "in your ocfl config"
        )
        raise ConfigError(f"--verify-ots requires [bitcoin] section {where}")
    source = wf_validation.chain_source(btc_factory(cfg))
    return ots.ChainCheck(source, cfg.bitcoin.confirmations)


def _emit_chain_results(
    err_out: Console, results: tuple[ots.ProofChainResult, ...]
) -> None:
    for r in results:
        name = r.proof.path.rpartition("/")[2]
        verified = r.verified
        match r.status:
            case ots.AttestationStatus.VERIFIED if verified and verified.block:
                outcome = (
                    f"confirmed ({verified.block.confirmations} confs, "
                    f"block {verified.height})"
                )
            case ots.AttestationStatus.MISMATCH:
                outcome = "mismatch"
            case ots.AttestationStatus.SHALLOW:
                outcome = "shallow"
            case _:
                outcome = "pending"
        err_out.print(f"  {r.proof.version} ({name}): {outcome}")


def _should_emit_progress(ctx: typer.Context) -> bool:
    if appctx.json_mode(ctx):
        return False
    typed = appctx.app_ctx(ctx)
    return not typed.quiet


def _emit(ctx: typer.Context, msg: str) -> None:
    if _should_emit_progress(ctx):
        appctx.err_console(ctx).print(_style.dim(msg))


def _object_ids_by_path(fs: FS) -> dict[str, str]:
    try:
        root = StorageRoot.open(cast("FileWalker", fs))
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
    err_out: Console,
    emit_progress: bool,
    fs: FS,
    object_ids: dict[str, str],
    report: ValidationReport,
) -> None:
    if not emit_progress:
        return
    errors_by_path: dict[str, int] = {}
    for e in report.errors:
        if e.path:
            errors_by_path[e.path] = errors_by_path.get(e.path, 0) + 1
    for obj_path, object_id in object_ids.items():
        count = errors_by_path.get(obj_path)
        if count is None:
            if report.truncated:
                err_out.print(f"  {object_id}: not validated (max errors reached)")
                continue
            err_out.print(f"  {object_id}: ok")
            continue
        suffix = "error" if count == 1 else "errors"
        err_out.print(f"  {object_id}: {count} {suffix}")
    for obj_path, count in errors_by_path.items():
        if obj_path in object_ids:
            continue
        suffix = "error" if count == 1 else "errors"
        err_out.print(f"  {_read_object_id(fs, obj_path)}: {count} {suffix}")


def _output_result(
    out: Console,
    err_out: Console,
    report: ValidationReport,
    json_mode: bool,
    suppress: frozenset[str],
    *,
    show_paths: bool,
) -> None:
    def message(f: Finding) -> str:
        if show_paths and f.path:
            return f"{f.path}: {f.render()}"
        return f.render()

    errors = report.errors
    warnings = [w for w in report.warnings if w.code not in suppress]
    infos = [i for i in report.infos if i.code not in suppress]
    if json_mode:
        output_json(
            {
                "valid": report.is_valid,
                "errors": [{"code": e.code, "message": message(e)} for e in errors],
                "warnings": [{"code": w.code, "message": message(w)} for w in warnings],
                "info": [{"code": i.code, "message": message(i)} for i in infos],
            }
        )
    else:
        for e in errors:
            err_out.print(error_line(f"[{e.code}] {message(e)}"))
        for w in warnings:
            err_out.print(_style.warning(f"Warning: [{w.code}] {message(w)}"))
        for i in infos:
            err_out.print(_style.dim(f"Info: [{i.code}] {message(i)}"))
        if report.is_valid:
            out.print(
                _style.valid(
                    "Valid" + (f" ({len(warnings)} warnings)" if warnings else "")
                )
            )


def _join(*parts: str) -> str:
    return "/".join(p for p in parts if p)
