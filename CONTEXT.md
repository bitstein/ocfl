# OCFL

Preservation storage for SNI's archive: OCFL objects on local disk or S3, with content and inventories timestamped to Bitcoin via OpenTimestamps.

## Language

### Storage

**Version State**:
The complete logical path→digest mapping of one object version, plus where its content can be found. Input to building a version.
_Avoid_: Stage (old lib name)

**Commit Plan**:
The intent of one commit: the object, the new version, the base and new inventory digests, the new inventory bytes and an optional inventory proof. It records no progress; the storage root does. Saved as `plan.json`.
_Avoid_: Update plan, transaction

**Claim**:
The atomic, create-only write that reserves version N of an object for one writer. Losing the claim means another writer owns vN.
_Avoid_: Lock, reservation

**Publish**:
Replacing the object's root inventory so a claimed version becomes the object's head. Readers see the version only after publish.
_Avoid_: Commit (as a storage step), finalize

**Stray Claim**:
A claimed version whose writer died before publish or revert, so it blocks later claims until it is removed.
_Avoid_: Orphan version

### Workflow

**Staging Area**:
The per-object workspace where a new version is drafted, timestamped and committed. One per object per storage root.
_Avoid_: Stage, workspace, staging dir

**Phase**:
The lifecycle position of a Staging Area (drafting → content-pending → content-upgraded → inventory-pending → inventory-upgraded → committed).
_Avoid_: Status, state

### Timestamping

**Timestamp Target**:
What a timestamp proves: either a version's content files or its inventory.
_Avoid_: Timestamp kind, mode

**Proof**:
An OpenTimestamps `.ots` file for one digest. **Final** when it carries a Bitcoin attestation; **pending** when it only has calendar commitments. A Staging Area keeps every proof it requested or received, keyed by digest, until commit or discard (ADR-0004).
_Avoid_: Timestamp file, receipt

**Attestation**:
A proof's claim that its digest was committed in a specific Bitcoin block. **Verified** once checked against a node; **shallow** when the block has too few confirmations to trust.
_Avoid_: Confirmation
