import json
from contextlib import contextmanager
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, cast

import typer
from rich.markup import escape

from ocfl_cli import _appctx as appctx
from ocfl_cli import _style
from ocfl_cli._common import open_root, output_json, resolve_root
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
    from collections.abc import Generator

    from rich.console import Console

    from ocfl_cli._appctx import AppCtx
    from ocfl_cli.config import Config
    from ocfl_cli.ports import BtcRpcFactory
    from ocfl_lib.fs import FS, FileWalker


class OtsMode(StrEnum):
    AUTO = "auto"
    LOCAL = "local"
    CHAIN = "chain"


def validate(
    ctx: typer.Context,
    object_id: Annotated[
        str | None, typer.Argument(help="Object ID to validate.")
    ] = None,
    no_check_digests: Annotated[
        bool,
        typer.Option("--no-check-digests", help="Skip content digest verification."),
    ] = False,
    ots_mode: Annotated[
        OtsMode,
        typer.Option(
            "--ots",
            help=escape(
                f"Checks of the {ots.EXTENSION_NAME} proofs: auto (chain when "
                "[bitcoin] is configured, else local), local (no Bitcoin node), "
                "or chain (against Bitcoin). Default: auto."
            ),
        ),
    ] = OtsMode.AUTO,
) -> None:
    """Validate an OCFL storage root or a specific object."""
    app = appctx.of(ctx)
    check_digests = not no_check_digests
    suppress = app.config.effective_suppress(app.archive)
    chain = _chain_for(app, ots_mode)

    if object_id is not None:
        _validate_object(app, object_id, check_digests, suppress, chain)
    else:
        _validate_root(app, check_digests, suppress, chain)


def _chain_for(app: AppCtx, mode: OtsMode) -> ots.ChainCheck | None:
    cfg = app.config
    match mode:
        case OtsMode.LOCAL:
            return None
        case OtsMode.CHAIN:
            return _chain_check(cfg, app.btc_rpc_factory)
        case OtsMode.AUTO:
            cfg.raise_if_broken("bitcoin")
            if cfg.bitcoin is None:
                return None
            return _chain_check(cfg, app.btc_rpc_factory)


def _validate_root(
    app: AppCtx,
    check_digests: bool,
    suppress: frozenset[str],
    chain: ots.ChainCheck | None,
) -> None:
    path = resolve_root(app.root)
    emit_progress = _should_emit_progress(app)
    _emit(app, f"Validating OCFL storage root: {path}")
    _emit(app, "Checking root structure (NAMASTE, layout, extensions)...")
    _emit(app, "Walking object hierarchy...")
    fs = app.fs_factory(path)
    object_ids = _object_ids_by_path(fs) if emit_progress else {}
    if emit_progress:
        app.err_console.print(_style.dim(f"  Found {len(object_ids)} objects"))
    digest_status = "content digests enabled" if check_digests else "digests skipped"
    _emit(app, f"Validating objects ({digest_status})...")

    if chain is not None:
        _emit(
            app,
            f"Verifying OTS attestations (min {chain.confirmations} confirmations)...",
        )
    else:
        _emit(app, "OTS proofs: local checks only (no Bitcoin node)")
    with _fail_if_chain_unavailable():
        report = validate_root(fs, "", check_digests=check_digests, chain=chain)
    emit_root_object_results(app.err_console, emit_progress, fs, object_ids, report)
    _output_result(
        app.console,
        app.err_console,
        report,
        app.json,
        suppress,
        show_paths=True,
    )
    if not report.is_valid:
        raise typer.Exit(1)


def _validate_object(
    app: AppCtx,
    object_id: str,
    check_digests: bool,
    suppress: frozenset[str],
    chain: ots.ChainCheck | None,
) -> None:
    root, fs = open_root(app)
    obj_path = root.object_path(object_id)

    _emit(app, f"Validating object: {object_id}")
    _emit(app, "Checking OCFL structure...")
    if check_digests:
        _emit(app, "Verifying content digests...")

    if not _ots_extension_present(fs, obj_path):
        chain = None
    elif chain is not None:
        _emit(
            app,
            f"Verifying OTS attestations (min {chain.confirmations} confirmations)...",
        )
    else:
        _emit(app, "OTS proofs: local checks only (no Bitcoin node)")
    with _fail_if_chain_unavailable():
        report = validate_object(fs, obj_path, check_digests=check_digests, chain=chain)
        if chain is not None and _should_emit_progress(app):
            _emit_chain_results(
                app.err_console, ots.check_object_chain(fs, obj_path, chain)
            )

    _output_result(
        app.console,
        app.err_console,
        report,
        app.json,
        suppress,
        show_paths=False,
    )
    if not report.is_valid:
        raise typer.Exit(1)


@contextmanager
def _fail_if_chain_unavailable() -> Generator[None]:
    try:
        yield
    except OcflError as e:
        raise ErrorContext(e, prefix="cannot verify OTS attestations: ") from e


def _ots_extension_present(fs: FS, obj_path: str) -> bool:
    return fs.exists(_join(obj_path, ots.EXTENSION_DIR))


def _chain_check(cfg: Config | None, btc_factory: BtcRpcFactory) -> ots.ChainCheck:
    if cfg is not None:
        cfg.raise_if_broken("bitcoin")
    if cfg is None or cfg.bitcoin is None:
        where = (
            f"in {cfg.path}"
            if cfg is not None and cfg.path is not None
            else "in your ocfl config"
        )
        raise ConfigError(f"--ots=chain requires [bitcoin] section {where}")
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


def _should_emit_progress(app: AppCtx) -> bool:
    return not (app.json or app.quiet)


def _emit(app: AppCtx, msg: str) -> None:
    if _should_emit_progress(app):
        app.err_console.print(_style.dim(msg))


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


def emit_root_object_results(
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
