"""ocfl stage command group."""

import os
from pathlib import Path
from typing import Annotated

import typer

from ocfl_cli._common import err, open_root
from ocfl_cli.staging import StagingStore

stage_app = typer.Typer(
    name="stage",
    help="Stage changes for OCFL objects.",
    no_args_is_help=True,
)

_DEFAULT_STAGING_ROOT = (
    Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
    / "ocfl-cli"
    / "staging"
)


@stage_app.callback()
def stage_callback(
    ctx: typer.Context,
    staging_root: Annotated[
        str | None,
        typer.Option(
            "--staging-root",
            envvar="OCFL_STAGING_ROOT",
            help="Path to staging directory.",
        ),
    ] = None,
) -> None:
    """Stage changes for OCFL objects."""
    ctx.ensure_object(dict)
    root = Path(staging_root) if staging_root else _DEFAULT_STAGING_ROOT
    root.mkdir(parents=True, exist_ok=True)
    ctx.obj["staging"] = StagingStore(root)


@stage_app.command()
def new(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID to stage.")],
    digest: Annotated[
        str | None,
        typer.Option("--digest", help="Digest algorithm (new objects only)."),
    ] = None,
) -> None:
    """Create a new staging area for an object."""
    from ocfl_lib import DigestAlgorithm
    from ocfl_lib.inventory_builder import InventoryBuilder

    store: StagingStore = ctx.obj["staging"]
    sr, _fs = open_root(ctx.obj.get("root"))
    root_path = str(ctx.obj.get("root") or Path.cwd().resolve())
    # Resolve to absolute for consistent staging path hashing
    root_path = str(Path(root_path).resolve())

    if store.exists(root_path, object_id):
        err(f"staging already exists for {object_id!r}")
        raise typer.Exit(1)

    obj = sr.get_object(object_id)
    if obj.exists:
        # Existing object: clone head inventory, add empty v(N+1)
        head_inv = obj.inventory.inventory
        builder = InventoryBuilder(head_inv).add_version(
            {}, created="1970-01-01T00:00:00Z"
        )
        inv = builder.build()
    else:
        # New object: build empty v1 inventory
        alg = DigestAlgorithm.SHA512
        if digest:
            try:
                alg = DigestAlgorithm.from_ocfl_name(digest)
            except (KeyError, ValueError):
                err(f"unknown digest algorithm: {digest!r}")
                raise typer.Exit(1) from None

        inv = (
            InventoryBuilder()
            .id(object_id)
            .spec(sr.spec)
            .digest_algorithm(alg)
            .add_version({}, created="1970-01-01T00:00:00Z")
            .build()
        )

    store.create(root_path, object_id, inv)
