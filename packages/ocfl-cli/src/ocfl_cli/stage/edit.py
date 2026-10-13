"""stage new, metadata, add, mv, rm, ls and reset: edit a Staging Area."""

from pathlib import Path
from typing import Annotated

import typer
from rich.text import Text

from ocfl_cli import _appctx as appctx
from ocfl_cli import _style
from ocfl_cli._common import open_root, output_json
from ocfl_cli.stage._app import (
    DigestOption,
    require_existing_staging,
    require_ots_config,
    resolve_root_path,
    stage_store,
)
from ocfl_cli.workflow import staging as wf_staging


def new(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID to stage.")],
    message: Annotated[
        str | None,
        typer.Option("-m", "--message", help="Version message metadata."),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option("--name", help="Version user name metadata."),
    ] = None,
    address: Annotated[
        str | None,
        typer.Option("--address", help="Version user address metadata."),
    ] = None,
    digest: Annotated[
        DigestOption | None,
        typer.Option("--digest", help="Digest algorithm (new objects only)."),
    ] = None,
    ots: Annotated[
        bool,
        typer.Option(
            "--ots",
            help="Enable OpenTimestamps workflow (requires SHA-256).",
        ),
    ] = False,
) -> None:
    """Create a new staging area for an object."""
    try:
        metadata = wf_staging.staged_metadata_from_options(
            message=message,
            name=name,
            address=address,
        )
    except wf_staging.MetadataInputError as e:
        raise typer.BadParameter(str(e)) from None
    cfg = appctx.config(ctx)
    ots_category_id = None
    if ots:
        ots_category_id = require_ots_config(cfg, digest).proof_category_id
        digest = DigestOption.SHA256

    store = stage_store(ctx)
    sr, _fs = open_root(ctx)
    digest_name = (
        digest.value
        if digest is not None
        else cfg.effective_digest(appctx.archive(ctx))
    )
    wf_staging.new_staging(
        store,
        sr,
        resolve_root_path(ctx),
        object_id,
        metadata=metadata,
        digest_name=digest_name,
        ots_category_id=ots_category_id,
    )


def metadata(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    message: Annotated[
        str | None,
        typer.Option("-m", "--message", help="Set version message metadata."),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option("--name", help="Set version user name metadata."),
    ] = None,
    address: Annotated[
        str | None,
        typer.Option("--address", help="Set version user address metadata."),
    ] = None,
    clear_message: Annotated[
        bool,
        typer.Option("--clear-message", help="Clear version message metadata."),
    ] = False,
    clear_name: Annotated[
        bool,
        typer.Option("--clear-name", help="Clear version user name metadata."),
    ] = False,
    clear_address: Annotated[
        bool,
        typer.Option("--clear-address", help="Clear version user address metadata."),
    ] = False,
) -> None:
    """Edit staged version metadata."""
    resolved = require_existing_staging(ctx, object_id, hint="run 'stage new' first")
    try:
        wf_staging.edit_metadata(
            resolved,
            object_id,
            message=message,
            name=name,
            address=address,
            clear_message=clear_message,
            clear_name=clear_name,
            clear_address=clear_address,
            runtime=appctx.runtime(ctx),
        )
    except wf_staging.MetadataInputError as e:
        raise typer.BadParameter(str(e)) from None


def add(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    src: Annotated[list[str], typer.Argument(help="Source file(s) or directory(ies).")],
    as_path: Annotated[
        str | None,
        typer.Option("--as", help="Logical path (file) or prefix (dir)."),
    ] = None,
) -> None:
    """Add files to a staged object version."""
    resolved = require_existing_staging(ctx, object_id, hint="run 'stage new' first")
    wf_staging.add_sources(
        resolved,
        object_id,
        [Path(s) for s in src],
        as_path=as_path,
        command=f"stage add {' '.join(src)}",
        runtime=appctx.runtime(ctx),
    )


def mv(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    src_path: Annotated[str, typer.Argument(help="Source logical path.")],
    dst_path: Annotated[str, typer.Argument(help="Destination logical path.")],
) -> None:
    """Rename a logical path in the staged version."""
    resolved = require_existing_staging(ctx, object_id)
    wf_staging.move_path(
        resolved, object_id, src_path, dst_path, runtime=appctx.runtime(ctx)
    )


def rm(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    paths: Annotated[list[str], typer.Argument(help="Logical path(s) to remove.")],
    recursive: Annotated[
        bool, typer.Option("-r", help="Remove all paths with matching prefix.")
    ] = False,
) -> None:
    """Remove logical paths from the staged version."""
    resolved = require_existing_staging(ctx, object_id)
    wf_staging.remove_paths(
        resolved, object_id, paths, recursive=recursive, runtime=appctx.runtime(ctx)
    )


def ls_cmd(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    digests: Annotated[
        bool, typer.Option("--digests", help="Show digests alongside paths.")
    ] = False,
) -> None:
    """List files in the staged head version."""
    resolved = require_existing_staging(ctx, object_id)
    entries = wf_staging.list_entries(resolved, object_id)

    if appctx.json_mode(ctx):
        if digests:
            output_json([{"path": p, "digest": d} for p, d in entries])
        else:
            output_json([p for p, _ in entries])
    else:
        for p, d in entries:
            if digests:
                appctx.console(ctx).print(Text.assemble(_style.digest(d), "  ", p))
            else:
                typer.echo(p)


def reset(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Discard persisted OTS progress (submissions, proofs, etc).",
        ),
    ] = False,
) -> None:
    """Delete staging area for an object."""
    removed = wf_staging.reset_staging(
        stage_store(ctx), resolve_root_path(ctx), object_id, force=force
    )
    if not removed:
        typer.echo(f"No staging exists for {object_id!r}", err=True)
