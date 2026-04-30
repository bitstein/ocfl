"""ocfl init command."""

import json
from typing import Annotated

import typer

from ocfl_cli._common import _make_fs, err, resolve_root
from ocfl_lib import Spec, StorageRoot, get_layout


def init(
    ctx: typer.Context,
    spec: Annotated[
        str, typer.Option("--spec", help="OCFL spec version (1.0 or 1.1).")
    ] = "1.1",
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
    root_path = resolve_root(ctx.obj.get("root"))

    try:
        ocfl_spec = Spec(spec)
    except ValueError:
        err(f"invalid spec: {spec!r}")
        raise typer.Exit(1) from None

    config = None
    if layout_config is not None:
        try:
            config = json.loads(layout_config)
        except json.JSONDecodeError as e:
            err(f"invalid layout config JSON: {e}")
            raise typer.Exit(1) from None

    try:
        layout_obj = get_layout(layout, config=config)
    except (KeyError, ValueError) as e:
        err(str(e))
        raise typer.Exit(1) from None

    try:
        fs = _make_fs(root_path)
        StorageRoot.init(
            fs,
            fs,
            spec=ocfl_spec,
            layout=layout_obj,
            layout_description=layout_description,
        )
    except (ValueError, OSError) as e:
        err(str(e))
        raise typer.Exit(1) from None
