---
status: accepted
---

# Commit by claim → publish; the plan records intent only

A commit writes many files and can die midway. The old Update Plan was a list of steps with run/revert closures and a "completed" flag. Closures can't be saved, so a reloaded plan was rebuilt from the inventory, which lost extension files, and the flag could disagree with the disk. We now follow ocfl-rs ADR 0006:
- The plan records only intent: object, vN, base and new inventory digests, and the new inventory bytes.
- The storage root is the only source of truth.
- Every recovery action is idempotent.

Each step order follows OCFL 1.1. The root inventory is replaced as the final operation (implementation notes §3.7.1). The root sidecar is the last write (spec §3.6). An object still being assembled stays where validators ignore it (§3.7.1).

## Order

**Local** (Linux only):
1. Lock.
2. Check the base.
3. Write the plan.
4. Build the complete `vN/` in the work dir: content, `inventory.json`, sidecar.
5. **Claim**: `renameat2(RENAME_NOREPLACE)` it into the object. The loser exits 3 and has written nothing into the object.
6. Write the inventory proof into the OTS extension dir.
7. **Publish**: replace the root inventory, then the root sidecar.
8. Clean up.

A new object is built whole in the work dir and claimed the same way. Missing layout parent dirs are created just before the claim.

**S3** (there is no rename):
- **Claim**: `PUT vN/inventory.json` with `If-None-Match: *`.
- Then write the content and the version sidecar, then the inventory proof.
- **Publish**: the root inventory with `If-Match: <base etag>`, then the root sidecar.
- New object: claim `v1/inventory.json` first, then the NAMASTE right after it, then content, then root inventory and sidecar last.
- The backend refuses to write if the store ignores conditional headers.

**Purge**: refuse while a claim is live or stray. Then delete in this order:
1. Root `inventory.json`.
2. Root sidecar.
3. `vN` dirs, highest first.
4. `extensions/` and `logs/`.
5. NAMASTE.
6. Empty parents.

Every partial state is either still an object or gone (no E072).

**Recover**: `recover OBJ --resume|--revert|--remove-stray VN` works out from disk which partial state it is in. It also handles "root inventory published, root sidecar stale" (E060) by finishing the sidecar. It removes empty layout parents (E073). `--remove-stray` also deletes that version's inventory proof.

## Considered Options

- **Steps as data + executor + completed-step journal.** This fixes serialization, but it keeps a second record that can disagree with the disk, and it does not stop two writers taking the same vN.
- **Local claim by `os.mkdir(vN)`, then fill it.** Portable, but a partial `vN/` sits in the object, and that is invalid (E046/E001) until published. Rejected because the spec advises assembling elsewhere.
- **`os.rename` of the built dir.** Rejected: it silently replaces an empty target.

## Consequences

- Local claims need Linux ≥3.15 and a filesystem that supports `RENAME_NOREPLACE` (ext4, btrfs, xfs, tmpfs). On any other platform or filesystem, the commit refuses to run; there is no fallback.
- On S3, a claimed but unpublished vN is visible. It is invalid under the spec until it is published or reverted. Validators report it with spec codes (E046, E058 for the missing sidecar). Tooling may add a "Stray Claim" hint, but never in place of the spec error.
- Local and S3 have different partial states, so recovery keeps one table per backend.
- The work dir lives under the storage root's `extensions/` as a documented local extension. It holds no empty dirs once the commit finishes (E073). Its name is settled together with the OTS extension name before launch.
