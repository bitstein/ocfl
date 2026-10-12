"""stage commit."""

from typing import Annotated

import typer

from ocfl_cli import _appctx as appctx
from ocfl_cli import errors as errors
from ocfl_cli._common import open_root
from ocfl_cli.errors import (
    ErrorContext,
    PhaseError,
    UsageError,
)
from ocfl_cli.stage._app import (
    require_existing_staging,
)
from ocfl_cli.stage.batch import commit_batch
from ocfl_cli.workflow import commit as wf_commit
from ocfl_cli.workflow import staging as wf_staging


def commit(
    ctx: typer.Context,
    object_id: Annotated[str | None, typer.Argument(help="Object ID.")] = None,
    batch_id: Annotated[
        str | None,
        typer.Option("--batch", help="Commit every object in an inventory batch."),
    ] = None,
    allow_missing_metadata: Annotated[
        bool,
        typer.Option(
            "--allow-missing-metadata",
            help="Commit even when staged version metadata is incomplete.",
        ),
    ] = False,
) -> None:
    """Commit staged changes to the storage root."""
    if (object_id is None) == (batch_id is None):
        raise UsageError("exactly one of OBJECT_ID or --batch is required")
    if batch_id is not None:
        if allow_missing_metadata:
            raise UsageError(
                "--allow-missing-metadata is not supported with --batch; "
                "commit those objects individually"
            )
        commit_batch(ctx, batch_id)
        return
    assert object_id is not None
    sr, fs = open_root(appctx.root(ctx))
    resolved = require_existing_staging(ctx, object_id)
    store = resolved.store
    root_path = resolved.root_path
    staging_dir = resolved.staging_dir

    # OTS commit path: ots.json makes this an OTS staging even when the
    # deferred inventory plan is missing or stale.
    if wf_staging.is_ots_staging(staging_dir):
        try:
            wf_commit.commit_ots(
                store=store,
                sr=sr,
                fs=fs,
                root_path=root_path,
                object_id=object_id,
                staging_dir=staging_dir,
                allow_missing_metadata=allow_missing_metadata,
                runtime=appctx.runtime(ctx),
            )
        except PhaseError as e:
            raise ErrorContext(
                e,
                suffix=(
                    "; run 'stage timestamp inventory' and then "
                    "'stage timestamp inventory --upgrade' to refresh the "
                    "timestamped inventory plan"
                ),
            ) from e
        return

    wf_commit.commit_bare(
        store=store,
        sr=sr,
        fs=fs,
        root_path=root_path,
        object_id=object_id,
        staging_dir=staging_dir,
        allow_missing_metadata=allow_missing_metadata,
        runtime=appctx.runtime(ctx),
    )
