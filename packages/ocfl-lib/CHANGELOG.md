# Changelog

All notable changes to `ocfl-lib`. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
No release is published yet, so every entry below is unreleased and may still change.

## [Unreleased]

### Public API

- Public modules are `ocfl_lib`, `ocfl_lib.fs`, `ocfl_lib.fs.s3`, `ocfl_lib.ots` and `ocfl_lib.validation`. Every other module is private (`_`-prefixed). Import from the public modules only.
- `ocfl_lib` exports every error class, `DiffEntry`, `to_path_map` and `CommitPlan`.
- `StorageRoot.commit` is the only way to write a version. No public function takes a `WriteFS` except the `StorageRoot` methods `init`, `commit` and `purge`.

### Commit protocol (ADR-0001)

- `StorageRoot.plan(id, version_state, *, spec, message, user, created)` returns a frozen `CommitPlan` and writes nothing. The plan holds only intent: object, version, base inventory digest, new inventory bytes and an optional `inventory_proof`. It holds no steps or progress.
- `CommitPlan.to_json()` / `CommitPlan.from_json()` read and write the new `plan.json` format (`planFormat` 1). `from_json` checks the inventory bytes against their digest and raises `PlanMismatchError` on any mismatch.
- `StorageRoot.commit(fsys, id, version, *, content=None, ...)` takes a `VersionState` or a `CommitPlan` (with `content=`). It refuses a plan whose base is no longer the object's head (`StaleHeadError`), a plan for another object, and an inventory proof that does not commit to the plan's sha256 inventory. A proof starts the `NNNN-ots-timestamps` extension of a new object (config and proof); an existing object must already use the extension. A commit also reverts on `KeyboardInterrupt`.
- Commits claim, then publish. Local (Linux): lock the object, build the complete `vN/` (or the whole new object) in `extensions/NNNN-work-dir/<sha256(id)>/` under the storage root, claim it with `renameat2(RENAME_NOREPLACE)`, write the inventory proof, then the root inventory, then the root sidecar. S3: claim with a create-only `vN/inventory.json` (`If-None-Match: *`), then content and version sidecar, the proof, the root inventory with `If-Match` on the base ETag, and the root sidecar last. A stale proof for vN is removed before publish. The work dir is a documented local extension (`docs/ext/NNNN-work-dir.md`) and is removed when empty.
- New errors: `ClaimLostError` (another writer claimed vN; nothing was written into the object), `RecoveryRequiredError` (an unfinished commit or stray claim blocks the commit), `ObjectLockedError` (busy object lock), `UnsupportedBackendError` (no `renameat2(RENAME_NOREPLACE)` on this platform or filesystem, or an S3 store that ignores conditional headers; there is no fallback).
- `StorageRoot.commit_status(id)` reads how far the object's last commit got: a `CommitStatus(state, version)` with `CommitState` `CLEAN`, `PLANNED`, `CLAIMED`, `INVENTORY_PUBLISHED` (root inventory replaced, root sidecar stale: E060), `PUBLISHED` (only the plan is left) or `STRAY` (a claim with no plan).
- `StorageRoot.recover(fsys, id, RecoveryAction, *, version=None, content=None)` finishes or undoes it from what is on storage, with one recovery table per backend. `RESUME` finishes a claimed version (on S3, missing content needs `content=`), and discards an unclaimed one. `REVERT` undoes an unpublished version and raises `RevertCompletedError` once the root inventory is published. `REMOVE_STRAY` removes the stray claim `version` and its inventory proof. Every action is idempotent and removes empty work dirs and empty layout parents (E073). New error: `RecoveryActionError` (the action does not apply to the state).
- `StorageRoot.purge` refuses while a commit of the object is live (`ObjectLockedError`), unfinished or stray (`RecoveryRequiredError`). It deletes in the ADR-0001 order: root inventory, root sidecar, version directories highest first, `extensions/` and `logs/`, the NAMASTE, then the empty object directory and layout parents. Every partial state is still an object or gone (never E072), and re-running purge finishes it.
- `LocalFS` is durable: a write fsyncs the file before its atomic rename and the directory after it; renames, removals, copies and new directories fsync their directories. A power loss leaves each file old or new.
- A commit checks every content file against its manifest digest while copying it (a server-side copy is read back). A mismatch raises `ContentDigestMismatch` and the commit is undone before anything is published.
- `ocfl_lib.fs` adds the `ClaimFS` (`rename_noreplace`, `lock`, `remove_empty_dir`) and `ConditionalFS` (`read_tagged`, `write_new`, `write_if_match`, `check_conditional_writes`) protocols. `LocalFS` implements `ClaimFS`; `S3FS` implements `ConditionalFS`.

### Changed

- Errors: every lib error subclasses `OcflError` and carries `error_class` (`ErrorClass`) and a stable snake_case `code`. Errors that replace a builtin raise keep that base (`ValueError`, `KeyError`). New: `ContentNotFound`, `LogicalPathNotFound`, `UnknownLayout`, `RootExistsError`, `UnknownInventoryType`, `InvalidVersionNumber`, `UnknownDigestAlgorithm`, `DigestAlgorithmMismatch`, `InvalidPath`, `PathCollision`, `InvalidLayoutConfig`, `InventoryBuildError`, `PlanMismatchError`.
- `Object` only reads. `Object.update`, `new_update_plan` and `apply_update_plan` are gone; write with `StorageRoot.commit`. `Object.path` and `Object.fs` are public.
- `Object.version_state(v)` returns a `VersionState`. Use `Object.version_info(v).state` for the digest map.
- `StorageRoot(fs, path, *, spec, layout)` does no I/O. Use `StorageRoot.open(fs, path, *, layouts=...)` to open an existing root.
- `Stage` is renamed `VersionState`, and `stage_dir` is renamed `scan_dir`. `ContentSource` and `FixitySource` live in their own module.
- `LayoutRegistry` is immutable: `with_layout` returns a new registry. `DEFAULT_LAYOUTS` holds the built-ins. Pass a custom registry with `layouts=` to `StorageRoot.open` or `get_layout`.
- `validate_object` and `validate_root` return a frozen `ValidationReport` of `Finding(code, severity, object_id, path, detail)`. Use `Finding.render()` for a display message. Root validation keeps every object's findings, including the warnings of valid objects.
- Validators take `JsonValue` / `JsonObject` instead of `dict[str, Any]`.
- `read_layout_config` returns `None` when the config is absent.

### Removed

- `ValidationResult`, `ValidationError` and `RootValidationSummary` (replaced by `ValidationReport`).
- `serialize_plan`, `deserialize_plan` and the step-list `plan.json` format (replaced by `CommitPlan.to_json` / `from_json`).
- `update_object`, `plan_update`, `apply_update_plan`, `prepare_steps`, `execute_plan`, `revert_plan` and `UpdatePlan` (use `StorageRoot.plan` and `StorageRoot.commit`).
- The step executor and `RevertError`. A failed undo is attached as a note to the original error.
- `fs.utils.exists` (use `FS.exists`), `LayoutRegistry.register`, and the unused `ExtensionError`, `CompanionMissingError` and `EmbeddedDigestMismatchError`.
