"""ocfl init command."""

import json
from typing import Annotated

import typer

from ocfl_cli import _appctx as appctx
from ocfl_cli._common import _make_fs, resolve_root
from ocfl_cli._exit import exit_for_error
from ocfl_lib import Spec, StorageRoot, get_layout


def init(
    ctx: typer.Context,
    spec: Annotated[
        Spec, typer.Option("--spec", help="OCFL spec version (1.0 or 1.1).")
    ] = Spec.V1_1,
    layout: Annotated[
        str,
        typer.Option("--layout", help="Storage layout extension name."),
    ] = "0004-hashed-n-tuple-storage-layout",
    layout_config: Annotated[
        str | None,
        typer.Option("--layout-config", help="Layout config as JSON string."),
    ] = None,
    layout_description: Annotated[
        str,
        typer.Option("--layout-description", help="Layout description text."),
    ] = "",
) -> None:
    """Initialize a new OCFL storage root."""
    root_path = resolve_root(appctx.root(ctx))

    config = None
    if layout_config is not None:
        try:
            config = json.loads(layout_config)
        except json.JSONDecodeError as e:
            exit_for_error(e, message=f"invalid layout config JSON: {e}")

    try:
        layout_obj = get_layout(layout, config=config)
    except (KeyError, ValueError) as e:
        exit_for_error(e)

    try:
        fs = _make_fs(root_path)
        StorageRoot.init(
            fs,
            fs,
            spec=spec,
            layout=layout_obj,
            layout_description=layout_description,
        )
    except (ValueError, OSError) as e:
        exit_for_error(e)
