---
status: accepted
---

# A Staging Area keeps its proofs in one table keyed by digest

A Proof commits to a digest, not to a path or a Phase. The old `ots.json` kept content proofs by digest but the inventory proof as one record tied to the staged inventory, dropped it (with its verify jobs and `plan.json`) whenever the Staging Area moved back, and only proved the first path of a digest. A file that was removed and added again, renamed, or changed and changed back sat as an "orphan" until it was submitted again, and an inventory rebuilt with the same bytes waited for Bitcoin a second time.

`ots.json` schema 2 has one `proofs` table, `{digest: {target, ots_path, verified, block_height, …}}`, for both Timestamp Targets:
- A digest gets an entry when it is submitted (requested, `verified: false`) and keeps it until commit or discard. Moving back to an earlier Phase never removes one, nor its verify jobs or cached `.ots` bytes.
- The digests the staged version needs are worked out from its state on every pass: every ordinary path needs its companion `P.ots`. `X.ots` is a content proof only when `X` is in the same state (`ocfl_lib.ots.split_state`); a standalone `x.ots` is ordinary and gets `x.ots.ots`.
- The object's own content proofs count too: a digest proved in any covered version of the object reuses that committed proof (its bytes are already in the manifest, and the commit that added them checked them). A plain object proves nothing, so its first covered version proves every file (`timestampFrom`).
- Every path of a digest with a final proof gets its companion, from the object or from the cached bytes, so a renamed or re-added file needs no submission. `stage timestamp` sends only digests with no entry; when every needed digest is final, the Phase moves on at once.
- The staged inventory (`inventory: {version, digest}`) is cleared when the Staging Area moves back; its proof stays. A rebuilt inventory first tries the last `plan.json`'s `created`: if those bytes already have an entry, they reuse it (a final one moves straight to inventory-upgraded). Otherwise the version is created now, so a version is never dated before content that changed after it.

## Considered Options

- **Key proofs by logical path.** Rejected: a proof is about bytes; paths move, and the extension spec already reads proofs by state digest.
- **Two tables (content by digest, inventory by version).** Rejected: the inventory proof is also just a proof of a digest, and two tables kept the two workflows apart (roadmap §6.1).
- **Reuse an inventory proof by trying every earlier `created`.** Rejected for now: one `created` covers edit → revert; more is bookkeeping for a rare case.
- **Migrate v1 files.** Rejected: there is no production archive yet, so a v1 Staging Area is discarded (`stage reset --force`) and staged again.

## Consequences

- `ots.json` schema 2 is not readable by older CLIs, and v1 is refused with a pointer to `stage reset --force`.
- A Staging Area's proofs table only grows until commit or discard.
- Every submission records its `target`.
- Validators are unaffected: the committed object is what the extension spec checks, and the lib still refuses a commit with a missing, invalid or pending content proof.
