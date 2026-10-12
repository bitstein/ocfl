# Architecture roadmap

Target architecture from the 2026-10-11 design session. Terms: [CONTEXT.md](../CONTEXT.md). Decisions: [docs/adr/](adr/).

## Rules for every step

- **OCFL compliance is not negotiable.** Every change must keep strict conformance with the OCFL 1.1 spec, its validation codes, the implementation notes and the community-extension rules. Tool conveniences never weaken what a validator reports.
- The official fixture corpus passes after every step, with no new xfails.
- Atomic commits: the tests pass after each one.
- There is no production archive yet (2026-10-11), so every format may change, with no migrations, until launch.

## 1. Bug fixes

- [x] `UpdatePlan.extension_files` is lost by `serialize`/`deserialize` (`update_plan.py` `_PlanState`)
- [x] `_resolve_root_path` calls `Path.resolve()` on `s3://` roots and corrupts the staging key (`stage/commands.py`)
- [x] An unregistered storage-root extension dir is reported as W901. Use the spec code W016 instead (`validation/root.py:124`).
- [x] A non-directory entry in the root `extensions/` is reported as E086, which does not exist in OCFL 1.1. Use E112 (`validation/root.py:122`).
- [x] L001/L002 (NUL bytes, length limits) are reported as errors, which makes objects that are valid under the spec invalid. Lower them to info. Validity is decided by E/W codes only.
- [x] Writing a 1.1 object into a `0=ocfl_1.0` root must be refused (E081). When a 1.1 version is added to a 1.0 object, swap the NAMASTE so exactly one remains (E003, E038).
- [x] Found on the way: the validator checked E103 against the root instead of the preceding version, so valid 1.0→1.1 upgrades failed. It also had no E081 check.

## 2. Lib cleanup

- [x] Errors: every lib error subclasses `OcflError` (it also inherits `ValueError`/`KeyError` where callers rely on that) and carries an `ErrorClass` and a stable `code`. Delete the unused classes. Export every class the public API can raise.
- [x] Public API: `__init__` plus `fs.s3`, `ots` and `validation` are public. Every other module gets a leading `_`. `Object.path` becomes public. Start a CHANGELOG.
- [x] `Object` only reads. Opening a root uses `StorageRoot.open(fs)`, and its constructor does no I/O.
- [x] Rename `Stage` → `VersionState`. Move `ContentSource` and `FixitySource` to their own module.
- [x] Validators accept a recursive `JsonValue` type and narrowing helpers instead of `dict[str, Any]`.
- [x] A frozen `ValidationReport` holds `Finding(code, severity, object_id, path, detail)`. Messages are rendered only for display. Root validation must keep every object's findings: it now drops the warnings of valid objects (`validation/root.py` `_walk_and_validate`).
- [x] The default layout registry is frozen, and custom registries are passed explicitly.
- [x] Remove duplicates: `_join` ×6, the version-number regex ×3, the supported-version sets, the layout-config helpers in `StorageRoot`, the multi-digest copies, and `fs_utils.exists`.

## 3. Commit protocol (ADR-0001)

- [ ] The plan records only the intent, with a new `plan.json` format.
- [ ] Local: build `vN/` (or a new object) in the work dir, then claim with `renameat2(RENAME_NOREPLACE)`. S3: claim with a create-only `vN/inventory.json`. Then the inventory proof, then the root inventory, then the root sidecar.
- [ ] Recovery tables, one per backend, including the stale-sidecar case (E060) and pruning empty parents (E073).
- [ ] `StorageRoot.commit(...)` is the only write entry point.
- [ ] Purge follows the ADR-0001 order and refuses to run over a live or stray claim.
- [ ] `recover OBJ --resume|--revert|--remove-stray VN`.
- [ ] A fake filesystem that fails at write N, plus a hypothesis state-machine test (commit → crash → recover) on local disk and on moto S3.

## 4. OTS extension into the lib (ADR-0002)

- [ ] Implement the revised extension spec [docs/ext/NNNN-ots-timestamps.md](ext/NNNN-ots-timestamps.md): honor the flags, a pending content proof is E907, inventory proofs are checked against their inventory (E909), and a standalone `x.ots` needs `x.ots.ots`. E903 is removed. Generate the E9xx/W9xx codes from the spec.
- [ ] Move the extension name, config, sidecar rules, the `.ots` validation walk and the S3 URI → FS factory into the lib.
- [ ] A `BlockHeaderSource` Protocol and a pure attestation check. A Bitcoin RPC client in the `[bitcoin]` extra.

## 5. CLI layers

- [ ] Layers: commands → use cases → adapters, enforced by import-linter. No `_private` imports across modules.
- [ ] Move the batch engine out of `commands.py` into the use cases.
- [ ] Ports for Clock, Sleeper and BlockStream (ZMQ). Fakes replace the monkeypatching of module internals.
- [ ] One error handler: `ErrorClass` → `ExitCode` with an exhaustive `match`, and JSON errors on stderr under `--json`.
- [ ] Create consoles per command run, in `AppCtx`, instead of `_style` globals.

## 6. Merged timestamp workflow

- [ ] One workflow, parameterized by the Timestamp Target, which is taken from the Phase. This replaces the content/inventory copies in `service`, `watch` and the batch engine.
- [ ] Proofs keyed by digest and kept when a Staging Area moves back to an earlier Phase. An `X.ots` counts only when `X` is in the same state.
- [ ] Share one execute/revert block between `commit_bare` and `commit_ots`.

## 7. CLI surface

- [ ] `stage timestamp|upgrade|retry OBJ [--expect]`. Batches become `stage batch create content|inventory …` plus `submit/upgrade/retry/commit/status/rm/ls`.
- [ ] `reset` → `discard`. Add `config path|show|check`.
- [ ] Locks: flock, a 5 s wait, then exit 4. The batch flock is the lease, and the heartbeat `.driver` is removed.
- [ ] Review the 37 issues in `ocfl-rs/docs/port/python-cli-surface.md` §6 one by one. Adopt only the ones that are justified on their own.

## Before launch

- [ ] Choose permanent names for the OTS extension (now the `NNNN-` placeholder, which gives W013) and the work-dir extension. Register them with the OCFL community, or document them as local extensions in the storage root (§4.5).
