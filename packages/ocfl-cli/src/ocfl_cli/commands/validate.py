"""ocfl validate command."""

from typing import Annotated

import typer

from ocfl_cli import _style
from ocfl_cli._common import _make_fs, err, open_root, output_json, resolve_root
from ocfl_lib import OcflError, ValidationResult, validate_object, validate_root


def validate(
    ctx: typer.Context,
    object_id: Annotated[
        str | None, typer.Argument(help="Object ID to validate.")
    ] = None,
    no_check_digests: Annotated[
        bool,
        typer.Option("--no-check-digests", help="Skip content digest verification."),
    ] = False,
) -> None:
    """Validate an OCFL storage root or a specific object."""
    json_mode = ctx.obj.get("json", False)
    check_digests = not no_check_digests

    if object_id is not None:
        _validate_object(ctx, object_id, check_digests, json_mode)
    else:
        _validate_root(ctx, check_digests, json_mode)


def _validate_root(ctx: typer.Context, check_digests: bool, json_mode: bool) -> None:
    path = resolve_root(ctx.obj.get("root"))
    try:
        fs = _make_fs(path)
    except (OcflError, OSError) as e:
        _fail(str(e), json_mode)

    result = ValidationResult()
    validate_root(fs, "", result, check_digests=check_digests)
    _output_result(result, json_mode)
    if not result.is_valid:
        raise typer.Exit(1)


def _validate_object(
    ctx: typer.Context, object_id: str, check_digests: bool, json_mode: bool
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
    _output_result(result, json_mode)
    if not result.is_valid:
        raise typer.Exit(1)


def _fail(msg: str, json_mode: bool) -> None:
    """Report an early failure and exit."""
    if json_mode:
        output_json({"valid": False, "errors": [msg], "warnings": []})
    else:
        err(msg)
    raise typer.Exit(1)


def _output_result(result: ValidationResult, json_mode: bool) -> None:
    if json_mode:
        output_json(
            {
                "valid": result.is_valid,
                "errors": [
                    {"code": e.code, "message": e.message} for e in result.errors
                ],
                "warnings": [
                    {"code": w.code, "message": w.message} for w in result.warnings
                ],
            }
        )
    else:
        for e in result.errors:
            err(f"[{e.code}] {e.message}")
        for w in result.warnings:
            _style.echo(
                _style.warning(f"Warning: [{w.code}] {w.message}"), err=True
            )
        if result.is_valid:
            _style.echo(
                _style.valid(
                    "Valid"
                    + (
                        f" ({result.num_warnings} warnings)"
                        if result.warnings
                        else ""
                    )
                )
            )
