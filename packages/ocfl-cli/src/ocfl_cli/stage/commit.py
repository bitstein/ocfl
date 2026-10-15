from typing import Annotated

import typer

from ocfl_cli._common import open_root
from ocfl_cli.errors import ErrorContext, PhaseError
from ocfl_cli.stage._app import require_existing_staging, stage_of
from ocfl_cli.workflow import commit as wf_commit
from ocfl_cli.workflow import staging as wf_staging
from ocfl_cli.workflow import status as wf_status


def commit(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    allow_missing_metadata: Annotated[
        bool,
        typer.Option(
            "--allow-missing-metadata",
            help="Commit even when staged version metadata is incomplete.",
        ),
    ] = False,
) -> None:
    """Commit staged changes to the storage root."""
    stage = stage_of(ctx)
    sr, fs = open_root(stage)
    resolved = require_existing_staging(stage, object_id)
    store = resolved.store
    root_path = resolved.root_path
    staging_dir = resolved.staging_dir

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
                runtime=stage.runtime,
            )
        except PhaseError as e:
            next_action = wf_status.next_step(store, root_path, object_id, staging_dir)
            raise ErrorContext(e, suffix=f"; next: {next_action}") from e
        return

    wf_commit.commit_bare(
        store=store,
        sr=sr,
        fs=fs,
        root_path=root_path,
        object_id=object_id,
        staging_dir=staging_dir,
        allow_missing_metadata=allow_missing_metadata,
        runtime=stage.runtime,
    )
