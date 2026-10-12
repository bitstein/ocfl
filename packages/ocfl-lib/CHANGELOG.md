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
- `StorageRoot.commit(fsys, id, version, *, content=None, ...)` takes a `VersionState` or a `CommitPlan` (with `content=`). It refuses a plan whose base is no longer the object's head (`StaleHeadError`), a plan for another object, and an inventory proof that does not commit to the plan's sha256 inventory. A proof starts the `NNNN-ots-timestamps` extension of an object that does not use it yet (see "OTS extension"). A commit also reverts on `KeyboardInterrupt`.
- Commits claim, then publish. Local (Linux): lock the object, build the complete `vN/` (or the whole new object) in `extensions/NNNN-work-dir/<sha256(id)>/` under the storage root, claim it with `renameat2(RENAME_NOREPLACE)`, write the inventory proof, then the root inventory, then the root sidecar. S3: claim with a create-only `vN/inventory.json` (`If-None-Match: *`), then content and version sidecar, the proof, the root inventory with `If-Match` on the base ETag, and the root sidecar last. A stale proof for vN is removed before publish. The work dir is a documented local extension (`docs/ext/NNNN-work-dir.md`) and is removed when empty.
- New errors: `ClaimLostError` (another writer claimed vN; nothing was written into the object), `RecoveryRequiredError` (an unfinished commit or stray claim blocks the commit), `ObjectLockedError` (busy object lock), `UnsupportedBackendError` (no `renameat2(RENAME_NOREPLACE)` on this platform or filesystem, or an S3 store that ignores conditional headers; there is no fallback).
- `StorageRoot.commit_status(id)` reads how far the object's last commit got: a `CommitStatus(state, version)` with `CommitState` `CLEAN`, `PLANNED`, `CLAIMED`, `INVENTORY_PUBLISHED` (root inventory replaced, root sidecar stale: E060), `PUBLISHED` (only the plan is left), `STRAY` (a claim with no plan) or `UNREADABLE_PLAN` (a corrupt `plan.json`, or one for another object; resume or revert discards it, and commit and purge refuse until then).
- `StorageRoot.recover(fsys, id, RecoveryAction, *, version=None, content=None)` finishes or undoes it from what is on storage, with one recovery table per backend. `RESUME` finishes a claimed version (on S3, missing content needs `content=`), and discards an unclaimed one. `REVERT` undoes an unpublished version and raises `RevertCompletedError` once the root inventory is published. `REMOVE_STRAY` removes the stray claim `version` and its inventory proof. Every action is idempotent and removes empty work dirs and empty layout parents (E073). New error: `RecoveryActionError` (the action does not apply to the state).
- `StorageRoot.purge` refuses while a commit of the object is live (`ObjectLockedError`), unfinished or stray (`RecoveryRequiredError`). It deletes in the ADR-0001 order: root inventory, root sidecar, version directories highest first, `extensions/` and `logs/`, the NAMASTE, then the empty object directory and layout parents. Every partial state is still an object or gone (never E072), and re-running purge finishes it.
- `LocalFS` is durable: a write fsyncs the file before its atomic rename and the directory after it; renames, removals, copies and new directories fsync their directories. A power loss leaves each file old or new.
- A commit checks every content file against its manifest digest while copying it (a server-side copy is read back). A mismatch raises `ContentDigestMismatch` and the commit is undone before anything is published.
- `ocfl_lib.fs` adds the `ClaimFS` (`rename_noreplace`, `lock`, `remove_empty_dir`) and `ConditionalFS` (`read_tagged`, `write_new`, `write_if_match`, `check_conditional_writes`) protocols. `LocalFS` implements `ClaimFS`; `S3FS` implements `ConditionalFS`.

### OTS extension (ADR-0002)

- Validation follows the revised `docs/ext/NNNN-ots-timestamps.md`. The extension is in use when its directory exists; a missing or invalid `config.json` is E908, and an unknown property is W904. `timestampContent`, `timestampInventory` and `timestampFrom` are honored: only covered versions are checked, a covered version needs `vN/inventory.json` (E909), and an inventory proof before `timestampFrom` is W905. Every inventory must use sha256 (E901).
- Content proofs: a standalone `x.ots` is ordinary content and needs `x.ots.ots` (E902). A content proof that does not parse, does not use a sha256 file hash or commits to another digest is E905; a pending one is E907 (was W902). E903 is removed.
- Inventory proofs: one that commits to the wrong digest is E905, also when `timestampInventory` is false; a pending one is W902.
- The E9xx/W9xx codes are generated from the spec's code table (`packages/ocfl-lib/scripts/gen_ots_codes.py`). Their detail keys are `version`, `path` and `proof`. `Finding.render` appends detail the template does not use, such as `reason`.
- `ocfl_lib.ots` adds `EXTENSION_NAME`, `OtsConfig` (`from_json`, `to_json`), `split_state` and `ParsedOts.file_hash` / `is_final`. New error: `OtsConfigError`.
- `ocfl_lib.ots` is the one home of the extension's rules: `EXTENSION_DIR`, `CONFIG_PATH`, `inventory_proof_path`, `read_config` (None when the extension directory is absent), `covered_versions`, and `object_proofs`, the `.ots` walk that lists every content and inventory proof of the covered versions as a `ProofRef(kind, version, path, digest)`. The commit, recovery, validation, ocfl-cli and ocfl-index use them. A new object's `config.json` now also states `proofDigestAlgorithm`.
- `ocfl_lib.fs.open_fs(location)` opens a local path or an `s3://bucket/prefix` URI (S3 needs the `[s3]` extra).
- Bitcoin checks (spec "Bitcoin Checks"): `ots.BlockHeaderSource` (a Protocol with `block_at(height) -> ChainBlock | None`) and the pure `ots.check_attestations(proof, source, confirmations=N)`, which marks each Bitcoin attestation `VERIFIED`, `SHALLOW` (no block yet, or fewer than N confirmations) or `MISMATCH`. `ParsedOts.attestations` lists them. `ots.check_object_chain` checks every proof of the covered versions, content proofs included. `validate_object(..., chain=ots.ChainCheck(source, confirmations))` reports a mismatch as E906 and an all-shallow proof as W903; a pending proof gets no Bitcoin finding, and errors from the source propagate instead of becoming findings.
- New `[bitcoin]` extra (`httpx2`) with the public `ocfl_lib.bitcoin.BitcoinRpcClient`, a `BlockHeaderSource` (`connect(url, user=, password=)`, `block_at`, `getblockchaininfo`, `getblockhash`, `getblockheader`). It retries transient faults and scrubs credentials from errors. New errors: `BitcoinRpcError`, `BitcoinRpcAuthError`, `BitcoinRpcTransientError`, `BitcoinNoBlockError`. The base install has no network code.
- `StorageRoot.commit` refuses, before it writes anything, a version that the object's extension config rejects by its inventory: no inventory proof when `timestampInventory` is set (`MissingInventoryProofError`, E904), a path without its content proof when `timestampContent` is set (`MissingContentProofError`, E902; this also applies when a proof starts a new object), a non-sha256 inventory (`PlanMismatchError`, E901), and an extension directory without a valid `config.json` (`OtsConfigError`, E908). A refused local commit no longer leaves an empty work dir (W016).
- An existing object can start timestamping: a commit with an inventory proof on an object without the extension writes `config.json` with `timestampFrom` set to the new version (a new object's config has none), after the claim and before the proof. Every path of that version needs its content proof, and every inventory of the object must use sha256 (E901). Revert and remove-stray remove a config whose `timestampFrom` is their version, its proof and the then-empty directories; `CommitState.CLAIMED` covers the new partial states (config only; config and proof).
- `StorageRoot.commit` checks every content proof of the new version before it writes anything, new ones read from the content source and carried-forward ones from the object: an invalid proof raises `InvalidProofError` (E905) and a pending one `PendingProofError` (E907). `ots.verify_proof(raw, expected_hex)` is the one proof check that validation and commit share.

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
