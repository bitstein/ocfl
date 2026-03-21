"""ocfl stage command group."""

import os
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from ocfl_cli._common import err, open_root
from ocfl_cli.staging import StagingStore

if TYPE_CHECKING:
    from ocfl_lib.fs.protocols import FS
    from ocfl_lib.inventory import Inventory

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


def _resolve_root_path(ctx: typer.Context) -> str:
    """Resolve absolute root path string for staging path hashing."""
    return str(Path(ctx.obj.get("root") or Path.cwd()).resolve())


def _update_head_state(inv: Inventory, state: dict[str, list[str]]) -> Inventory:
    """Return new Inventory with updated head version state."""
    from ocfl_lib.inventory import Inventory, VersionInfo

    head = inv.head
    vi = inv.versions[head]
    updated_versions = dict(inv.versions)
    updated_versions[head] = VersionInfo(
        created=vi.created, state=state, message=vi.message, user=vi.user
    )
    return Inventory(
        id=inv.id,
        type=inv.type,
        digest_algorithm=inv.digest_algorithm,
        head=inv.head,
        manifest=inv.manifest,
        versions=updated_versions,
        content_directory=inv.content_directory,
        fixity=inv.fixity,
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
    root_path = _resolve_root_path(ctx)

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
            except KeyError, ValueError:
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


@stage_app.command()
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
    from ocfl_lib import DigestAlgorithm
    from ocfl_lib.digest import file_digest

    store: StagingStore = ctx.obj["staging"]
    root_path = _resolve_root_path(ctx)

    if not store.exists(root_path, object_id):
        err(f"no staging exists for {object_id!r} (run 'stage new' first)")
        raise typer.Exit(1)

    inv = store.load_inventory(root_path, object_id)
    alg = DigestAlgorithm.from_ocfl_name(inv.digest_algorithm)
    head = inv.head
    state = dict(inv.versions[head].state)
    content_map = store.load_content_map(root_path, object_id)

    # Build reverse map: path → digest for removing overwritten paths
    path_to_digest: dict[str, str] = {}
    for d, paths in state.items():
        for p in paths:
            path_to_digest[p] = d

    # Set of digests already in prior manifest (don't need content.json entry)
    prior_digests = set(inv.manifest.keys())

    def _add_file(file_path: Path, logical_path: str) -> None:
        d = file_digest(file_path, alg)
        stat = file_path.stat()

        # Remove old digest mapping if overwriting this logical path
        if logical_path in path_to_digest:
            old_d = path_to_digest[logical_path]
            old_paths = state.get(old_d, [])
            if logical_path in old_paths:
                old_paths.remove(logical_path)
                if not old_paths:
                    state.pop(old_d, None)

        # Add to state
        if d in state:
            if logical_path not in state[d]:
                state[d].append(logical_path)
        else:
            state[d] = [logical_path]
        path_to_digest[logical_path] = d

        # Add to content map if not already in prior manifest
        if d not in prior_digests and d not in content_map:
            content_map[d] = {
                "path": str(file_path.resolve()),
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            }

    for s in src:
        p = Path(s)
        if p.is_file() and not p.is_symlink():
            logical = as_path or p.name
            _add_file(p, logical)
        elif p.is_dir():
            for child in sorted(p.rglob("*")):
                if child.is_symlink():
                    continue
                if not child.is_file():
                    continue
                rel = str(child.relative_to(p))
                logical = f"{as_path}/{rel}" if as_path else rel
                _add_file(child, logical)

    store.save_inventory(root_path, object_id, _update_head_state(inv, state))
    store.save_content_map(root_path, object_id, content_map)


@stage_app.command()
def mv(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    src_path: Annotated[str, typer.Argument(help="Source logical path.")],
    dst_path: Annotated[str, typer.Argument(help="Destination logical path.")],
) -> None:
    """Rename a logical path in the staged version."""
    store: StagingStore = ctx.obj["staging"]
    root_path = _resolve_root_path(ctx)

    if not store.exists(root_path, object_id):
        err(f"no staging exists for {object_id!r}")
        raise typer.Exit(1)

    inv = store.load_inventory(root_path, object_id)
    state = dict(inv.versions[inv.head].state)

    # Find which digest owns src_path
    found_digest = None
    for d, paths in state.items():
        if src_path in paths:
            found_digest = d
            break

    if found_digest is None:
        err(f"path not found: {src_path!r}")
        raise typer.Exit(1)

    # Rename: remove src, add dst
    state[found_digest].remove(src_path)
    if not state[found_digest]:
        state[found_digest] = [dst_path]
    else:
        state[found_digest].append(dst_path)

    store.save_inventory(root_path, object_id, _update_head_state(inv, state))


@stage_app.command()
def rm(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    paths: Annotated[list[str], typer.Argument(help="Logical path(s) to remove.")],
    recursive: Annotated[
        bool, typer.Option("-r", help="Remove all paths with matching prefix.")
    ] = False,
) -> None:
    """Remove logical paths from the staged version."""
    store: StagingStore = ctx.obj["staging"]
    root_path = _resolve_root_path(ctx)

    if not store.exists(root_path, object_id):
        err(f"no staging exists for {object_id!r}")
        raise typer.Exit(1)

    inv = store.load_inventory(root_path, object_id)
    state = {d: list(ps) for d, ps in inv.versions[inv.head].state.items()}
    content_map = store.load_content_map(root_path, object_id)

    removed_any = False
    for target in paths:
        if recursive:
            prefix = target.rstrip("/") + "/"
            for d in list(state):
                before = len(state[d])
                state[d] = [
                    p for p in state[d] if p != target and not p.startswith(prefix)
                ]
                if len(state[d]) < before:
                    removed_any = True
                if not state[d]:
                    del state[d]
        else:
            found = False
            for d in list(state):
                if target in state[d]:
                    state[d].remove(target)
                    if not state[d]:
                        del state[d]
                    found = True
                    removed_any = True
                    break
            if not found:
                err(f"path not found: {target!r}")
                raise typer.Exit(1)

    if not removed_any:
        err("no matching paths found")
        raise typer.Exit(1)

    # Clean unreferenced digests from content map
    referenced_digests = set(state.keys())
    for d in list(content_map):
        if d not in referenced_digests:
            del content_map[d]

    store.save_inventory(root_path, object_id, _update_head_state(inv, state))
    store.save_content_map(root_path, object_id, content_map)


@stage_app.command("ls")
def ls_cmd(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    digests: Annotated[
        bool, typer.Option("--digests", help="Show digests alongside paths.")
    ] = False,
) -> None:
    """List files in the staged head version."""
    store: StagingStore = ctx.obj["staging"]
    root_path = _resolve_root_path(ctx)

    if not store.exists(root_path, object_id):
        err(f"no staging exists for {object_id!r}")
        raise typer.Exit(1)

    inv = store.load_inventory(root_path, object_id)
    state = inv.versions[inv.head].state

    # Collect (path, digest) pairs sorted by path
    entries: list[tuple[str, str]] = []
    for d, paths in state.items():
        for p in paths:
            entries.append((p, d))
    entries.sort()

    if ctx.obj.get("json"):
        from ocfl_cli._common import output_json

        if digests:
            output_json([{"path": p, "digest": d} for p, d in entries])
        else:
            output_json([p for p, _ in entries])
    else:
        for p, d in entries:
            if digests:
                typer.echo(f"{d}  {p}")
            else:
                typer.echo(p)


@stage_app.command()
def status(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
) -> None:
    """Show changes in staged version vs committed head."""
    from ocfl_lib.digest_map import to_path_map
    from ocfl_lib.spec import VNum

    store: StagingStore = ctx.obj["staging"]
    root_path = _resolve_root_path(ctx)

    if not store.exists(root_path, object_id):
        err(f"no staging exists for {object_id!r}")
        raise typer.Exit(1)

    inv = store.load_inventory(root_path, object_id)
    head = inv.head

    # Get staged state and prior state (version before head)
    staged_pm = to_path_map(inv.versions[head].state)
    head_vnum = VNum.parse(head)
    if head_vnum.number > 1:
        prev_v = str(VNum(head_vnum.number - 1))
        prior_pm = to_path_map(inv.versions[prev_v].state)
    else:
        prior_pm: dict[str, str] = {}

    # Compute diff
    all_paths = set(staged_pm) | set(prior_pm)
    changes: list[dict[str, str]] = []
    added: dict[str, str] = {}
    removed: dict[str, str] = {}

    for p in sorted(all_paths):
        in_prior = p in prior_pm
        in_staged = p in staged_pm
        if in_staged and not in_prior:
            added[p] = staged_pm[p]
        elif in_prior and not in_staged:
            removed[p] = prior_pm[p]
        elif prior_pm[p] != staged_pm[p]:
            changes.append({"path": p, "action": "modified"})

    # Rename detection: pair added/removed sharing a digest
    removed_by_digest: dict[str, list[str]] = {}
    for p, d in removed.items():
        removed_by_digest.setdefault(d, []).append(p)

    for p, d in sorted(added.items()):
        if removed_by_digest.get(d):
            old_path = removed_by_digest[d].pop(0)
            changes.append({"path": p, "action": "renamed", "renamed_from": old_path})
            removed.pop(old_path, None)
        else:
            changes.append({"path": p, "action": "added"})

    for p in sorted(removed):
        changes.append({"path": p, "action": "deleted"})

    changes.sort(key=lambda c: c["path"])

    if ctx.obj.get("json"):
        from ocfl_cli._common import output_json

        output_json(changes)
    else:
        action_prefix = {"added": "A", "deleted": "D", "modified": "M", "renamed": "R"}
        for c in changes:
            prefix = action_prefix.get(c["action"], "?")
            line = f"{prefix} {c['path']}"
            if c.get("renamed_from"):
                line += f" (from {c['renamed_from']})"
            typer.echo(line)


@stage_app.command()
def commit(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
    message: Annotated[
        str | None, typer.Option("-m", "--message", help="Version message.")
    ] = None,
    name: Annotated[str | None, typer.Option("--name", help="Creator name.")] = None,
    address: Annotated[
        str | None, typer.Option("--address", help="Creator address.")
    ] = None,
    created: Annotated[
        str | None, typer.Option("--created", help="Created timestamp (ISO 8601).")
    ] = None,
) -> None:
    """Commit staged changes to the storage root."""
    from ocfl_lib import DigestAlgorithm, LocalFS
    from ocfl_lib.inventory import UserInfo
    from ocfl_lib.stage import Stage

    store: StagingStore = ctx.obj["staging"]
    sr, fs = open_root(ctx.obj.get("root"))
    root_path = _resolve_root_path(ctx)

    if not store.exists(root_path, object_id):
        err(f"no staging exists for {object_id!r}")
        raise typer.Exit(1)

    inv = store.load_inventory(root_path, object_id)
    content_map = store.load_content_map(root_path, object_id)
    alg = DigestAlgorithm.from_ocfl_name(inv.digest_algorithm)
    head = inv.head

    # Pre-commit validation: verify all content.json files still exist and are unchanged
    for _digest, entry in content_map.items():
        file_path = Path(entry["path"])
        if not file_path.exists():
            err(f"source file missing: {file_path}")
            raise typer.Exit(1)
        stat = file_path.stat()
        if stat.st_size != entry["size"] or stat.st_mtime != entry["mtime"]:
            err(f"source file changed since staging: {file_path}")
            raise typer.Exit(1)

    # Build ContentSource backed by content.json local file references
    class _StagingContentSource:
        def __init__(self, cm: dict, root: Path) -> None:
            self._cm = cm
            self._root = root

        def get_content(self, digest: str) -> tuple[FS, str]:
            if digest in self._cm:
                file_path = Path(self._cm[digest]["path"])
                local_fs = LocalFS(file_path.parent)
                return local_fs, file_path.name
            raise KeyError(digest)

    content_source = _StagingContentSource(content_map, Path(root_path))

    # Build Stage from staged inventory head state
    state = inv.versions[head].state
    stage_obj = Stage(
        state=state,
        digest_algorithm=alg,
        content_source=content_source,
    )

    user = None
    if name:
        user = UserInfo(name=name, address=address)

    try:
        sr.commit(
            fs,
            object_id,
            stage_obj,
            message=message,
            user=user,
            created=created,
        )
    except KeyboardInterrupt:
        err("interrupted, reverting...")
        raise typer.Exit(1) from None
    except Exception as exc:
        err(str(exc))
        raise typer.Exit(1) from None

    # Purge staging on success
    store.purge(root_path, object_id)


@stage_app.command()
def reset(
    ctx: typer.Context,
    object_id: Annotated[str, typer.Argument(help="Object ID.")],
) -> None:
    """Delete staging area for an object."""
    store: StagingStore = ctx.obj["staging"]
    root_path = _resolve_root_path(ctx)

    if not store.exists(root_path, object_id):
        typer.echo(f"No staging exists for {object_id!r}", err=True)
        return

    store.purge(root_path, object_id)
