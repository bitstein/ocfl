# OCFL Local Extension NNNN: OpenTimestamps Timestamps

  * **Extension Name:** `NNNN-ots-timestamps`
  * **Authors:** Michael Goldstein (Satoshi Nakamoto Institute)
  * **Minimum OCFL Version:** 1.1
  * **OCFL Community Extensions Version:** 1.0
  * **Related to:** n/a

## Overview

This object extension anchors the content and the versions of an OCFL
object to the Bitcoin blockchain with
[OpenTimestamps](https://opentimestamps.org/) (OTS) proofs. A proof shows
that the bytes it commits to existed at or before a given Bitcoin block. The
proofs are portable, and anyone can verify them against Bitcoin without
trusting the archive or the service that made them.

The extension uses two kinds of proof:

1. **Content proofs.** For a content file with logical path `P`, a file with
   logical path `P.ots` in the same version holds the proof for `P`. Content
   proofs are ordinary OCFL content. `P` and `P.ots` together are enough to
   verify `P` without revealing any other file in the object.
2. **Inventory proofs.** For each version `vN`, the file
   `inventory.json.vN.ots` in the extension directory holds the proof for the
   bytes of `vN/inventory.json`. That inventory lists the digests of all
   content in the version, including the content proofs, so the inventory
   proof anchors the whole state of the object at `vN`.

A content proof is required to be anchored in Bitcoin before its version is
committed. A proof that is added after a version is committed could be dated
long after the content, and would make a weaker claim.

The extension does not record how, or from which service, a proof was
obtained. Verification depends only on the proof and on Bitcoin.

## Parameters

### Summary

* **Name:** `proofDigestAlgorithm`
  * **Description:** The digest algorithm of the file hash that each proof
    commits to
  * **Type:** string
  * **Constraints:** Must be `sha256`, and must equal the `digestAlgorithm`
    of every inventory of the object
  * **Default:** `sha256`
* **Name:** `timestampContent`
  * **Description:** Whether every content file must have a content proof
  * **Type:** boolean
  * **Constraints:** n/a
  * **Default:** `true`
* **Name:** `timestampInventory`
  * **Description:** Whether every covered version must have an inventory
    proof
  * **Type:** boolean
  * **Constraints:** n/a
  * **Default:** `true`
* **Name:** `timestampFrom`
  * **Description:** The first version of the object that this extension
    covers
  * **Type:** string
  * **Constraints:** Must be the name of a version of the object, exactly as
    it appears in the inventory (for example `v3` or `v003`)
  * **Default:** the first version of the object

### Details

#### proofDigestAlgorithm

The algorithm of the file-hash operation at the start of every proof in the
object. The only allowed value in this version of the extension is
`sha256`: OpenTimestamps has no SHA-512 operation, and OCFL allows only
`sha512` or `sha256` as an inventory digest algorithm. Because the value
must equal the `digestAlgorithm` of the root inventory and of every version
inventory, a validator can compare a proof's file digest with the digest
already recorded in the inventory, without hashing the file again. The
parameter exists so that a later extension can allow other algorithms.

#### timestampContent

When `true`, the rules in [Content Proofs](#content-proofs) apply to every
covered version. When `false`, the extension makes no claim about content,
and files whose names end in `.ots` are ordinary content with no special
meaning.

#### timestampInventory

When `true`, the rules in [Inventory Proofs](#inventory-proofs) apply to
every covered version. When `false`, inventory proofs are not required, but
an inventory proof that is present must still be correct.

#### timestampFrom

The extension covers the version named by `timestampFrom` and every later
version (the **covered versions**). Earlier versions are not checked. This
lets an archive start timestamping an object that already has versions,
without making those versions invalid.

The first covered version needs a content proof for every file in its
state, including files carried forward from earlier versions, so each of
those proofs shows that the file existed at the time of that version.

A client MUST NOT change `timestampFrom` once the version it names exists.
Because `config.json` is not timestamped, a validator also reports a
warning when an inventory proof exists for a version before
`timestampFrom` (see [Local Checks](#local-checks)); that is what a moved
value looks like.

## Specification

### Terminology

* **Proof:** an OpenTimestamps detached timestamp file (`.ots`).
* **Final proof:** a proof that holds at least one
  `BitcoinBlockHeaderAttestation`.
* **Pending proof:** a proof that holds no `BitcoinBlockHeaderAttestation`
  (for example, only `PendingAttestation`s).
* **Versions of the object:** the versions listed in the `versions` block of
  the object's root `inventory.json`. A version directory that is not listed
  there (for example, one that a client is still writing) is not covered by
  this extension.
* **Covered versions:** the versions of the object from `timestampFrom`
  on.
* **Content proof:** in the state of a version, a logical path `P.ots`
  whose companion `P` is in the same state.
* **Companion:** for a logical path `P.ots`, the logical path `P`.
* **Ordinary path:** in the state of a version, any logical path that is
  not a content proof. This includes a path that ends in `.ots` but whose
  companion is not in the state.

The key words MUST, MUST NOT, SHOULD and MAY are to be interpreted as
described in [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119).

### Object Layout

When this extension is in use, an object root looks like this:

```
<object-root>/
├── 0=ocfl_object_1.1
├── extensions/
│   └── NNNN-ots-timestamps/
│       ├── config.json
│       ├── inventory.json.v1.ots
│       └── inventory.json.v2.ots
├── inventory.json
├── inventory.json.sha256
├── v1/
│   ├── inventory.json
│   ├── inventory.json.sha256
│   └── content/
│       ├── report.pdf
│       └── report.pdf.ots
└── v2/
    ├── inventory.json
    ├── inventory.json.sha256
    └── content/
        ├── photo.jpg
        └── photo.jpg.ots
```

### Activation and Configuration

The extension is in use for an object when the directory
`extensions/NNNN-ots-timestamps/` exists in the object root. That directory
MUST contain a `config.json` file, which MUST be a JSON object with the
property `extensionName` set to `NNNN-ots-timestamps`. The parameters are
serialized in the same object. Parameters that have their default values MAY
be omitted, so the smallest valid configuration is:

```json
{
  "extensionName": "NNNN-ots-timestamps"
}
```

A parameter with the wrong JSON type is an error. A property that this
extension does not define is a warning.

Activation depends on the directory, not only on `config.json`. The
extension directory is not listed in any inventory, so `config.json` is not
timestamped; if its absence switched the extension off, deleting it would
switch off every check.

The extension applies to the covered versions. To start timestamping an
object that already has versions, a client sets `timestampFrom` to the
first new version. A client MUST NOT remove the extension from an object,
or set either flag from `true` to `false`, once a covered version exists.

### Version Inventories

Every covered version MUST have a `vN/inventory.json`. The OCFL
specification only recommends this (W010). This extension requires it,
because the inventory proof of version `vN` commits to those bytes.

### Content Proofs

When `timestampContent` is `true`, these rules apply to the `state` of every
covered version:

1. For each ordinary path `P`, the logical path `P.ots` MUST be in the same
   state (it is then the content proof of `P`).
2. For each content proof `P.ots`, its content MUST be a valid proof whose
   file-hash operation is `proofDigestAlgorithm` and whose file digest
   equals the state digest of `P`.
3. The content proof MUST be final. A pending content proof in any covered
   version is an error.

A proof does not itself need a proof. Every other file does, including an
`.ots` file whose companion is not in the state: a standalone `x.ots` is
ordinary content, and its proof is `x.ots.ots`. So an archive can keep
`.ots` files of its own (for example, historical proofs) as ordinary
content.

These rules use logical paths and state digests, not content paths, so they
hold for renamed files, deduplicated content and files carried forward from
earlier versions. Every content file in a covered version is either proved
or is a proof.

### Inventory Proofs

When `timestampInventory` is `true`:

1. For each covered version `vN`, the file
   `extensions/NNNN-ots-timestamps/inventory.json.vN.ots` MUST exist, where
   `vN` is the version name exactly as it appears in the inventory (for
   example `v1` or `v001`).
2. The proof MUST be a valid proof whose file-hash operation is
   `proofDigestAlgorithm` and whose file digest equals the digest of the
   bytes of `vN/inventory.json`. This is the same value as the digest in
   `vN/inventory.json.sha256`.
3. A pending inventory proof is a warning, not an error.

Rule 2 also applies to an inventory proof that is present when
`timestampInventory` is `false`.

Inventory proofs are not OCFL content. They are not in any manifest or
state, and no inventory digest covers them. A client MAY therefore replace
the inventory proof of a covered version in place, but only with a
proof for the same inventory bytes that holds more attestations (for
example, after an upgrade). A client MUST NOT replace it with a proof that
commits to other bytes.

A file `inventory.json.vN.ots` for a `vN` that is not a version of the object
is ignored. A client that later creates version `vN` MUST first remove that
file or replace it with the proof for its own `vN/inventory.json`.

## Validation

### Local Checks

A validator of this extension MUST, without network access:

1. Check that `config.json` exists and is valid
   ([Activation and Configuration](#activation-and-configuration)).
2. Check that `proofDigestAlgorithm` is `sha256` and equals the
   `digestAlgorithm` of the root inventory and of every version inventory.
3. Check that `timestampFrom` names a version of the object, and that
   every covered version has `vN/inventory.json`.
4. If `timestampContent` is `true`, check content-proof rules 1–3 for every
   covered version.
5. If `timestampInventory` is `true`, check that every covered version has
   its inventory proof. Check inventory-proof rule 2 for every inventory
   proof of a covered version that is present.
6. Report a pending inventory proof as a warning.
7. Report a warning if an inventory proof exists for a version before
   `timestampFrom`.

A validator MAY let its user turn off the digest checks (content-proof rule
2 and inventory-proof rule 2), in the same way that it may let its user turn
off content fixity checks.

### Bitcoin Checks

A validator MAY also verify proofs against a Bitcoin node, under the
control of a client option. The client chooses a required number of
confirmations, `N`. For every proof in the object (content proofs and
inventory proofs of covered versions), and for each
`BitcoinBlockHeaderAttestation` at height
`H` in it, the validator gets the header of the block at height `H` on the
node's best chain:

* If the node has no block at height `H`, or the block has fewer than `N`
  confirmations, the attestation is **shallow**. A shallow attestation is
  not used and is not an error, because the block can still change.
* If the block has at least `N` confirmations and the commitment of the
  attestation equals the block's merkle root, the attestation is
  **verified**.
* If the block has at least `N` confirmations and the commitment does not
  equal the block's merkle root, that is an **error**. It indicates
  corruption, tampering, or a chain reorganization deeper than `N` blocks.

A proof passes when at least one attestation is verified and none is an
error. A proof whose Bitcoin attestations are all shallow gets a warning. A
pending proof gets no finding from these checks; the local checks already
report it. Attestations of other types are ignored by these checks and do
not make a proof final. A client SHOULD report the lowest verified height as
the time of the proof.

### Validation Codes

A validator of this extension SHOULD report its findings with these codes.
They are specific to this extension and do not collide with the codes of
the OCFL specification (`E001`–`E112`, `W001`–`W016`).

| Code | Level | Finding |
|---|---|---|
| E901 | error | `proofDigestAlgorithm` is not `sha256`, or an inventory of the object uses another `digestAlgorithm`. |
| E902 | error | Version `vN`, ordinary path `P`: no content proof `P.ots` in the state. |
| E904 | error | Version `vN`: the inventory proof `inventory.json.vN.ots` is missing. |
| E905 | error | Proof `X` does not parse, uses the wrong file-hash operation, or commits to the wrong digest. |
| E906 | error | Proof `X`: an attestation at a block with at least `N` confirmations does not match the block's merkle root. |
| E907 | error | Version `vN`: the content proof `P.ots` is pending. |
| E908 | error | The extension directory has no `config.json`, or `config.json` is not a JSON object, has the wrong `extensionName`, has a parameter of the wrong type, or `timestampFrom` does not name a version of the object. |
| E909 | error | Version `vN` has no `vN/inventory.json`. |
| W902 | warning | Version `vN`: the inventory proof is pending. |
| W903 | warning | Proof `X`: every Bitcoin attestation is shallow. |
| W904 | warning | `config.json` has a property that this extension does not define. |
| W905 | warning | An inventory proof exists for version `vK`, which is before `timestampFrom`. |

Code E903 is not used.

## Examples

### Example 1: a valid object

`extensions/NNNN-ots-timestamps/config.json`:

```json
{
  "extensionName": "NNNN-ots-timestamps",
  "proofDigestAlgorithm": "sha256",
  "timestampContent": true,
  "timestampInventory": true
}
```

The state of `v1` in the inventory (digests of the `.ots` files shortened):

```json
"state": {
  "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03": ["hello.txt"],
  "0c5f…": ["hello.txt.ots"]
}
```

The proof `hello.txt.ots` starts with a SHA-256 file-hash operation, its
file digest is `5891b5b5…6be03` (the state digest of `hello.txt`), and it
holds a `BitcoinBlockHeaderAttestation`. The file
`extensions/NNNN-ots-timestamps/inventory.json.v1.ots` is a proof whose file
digest equals the SHA-256 digest of the bytes of `v1/inventory.json`. The
object passes the local checks.

In `v2` the file is renamed to `greeting.txt`. Its content is not copied
again, and its proof is renamed with it:

```json
"state": {
  "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03": ["greeting.txt"],
  "0c5f…": ["greeting.txt.ots"]
}
```

`v2` passes, because the rules use logical paths and state digests.

### Example 2: an invalid object

The state of `v1` of another object:

```json
"state": {
  "cab46e42152f6e7dffc7b6cf55eb281717343e72667f6fa64d2cc479a9d8f825": ["photo.jpg"],
  "91d2…": ["photo.jpg.ots"],
  "7a1e…": ["notes.ots"]
}
```

* If `photo.jpg.ots` holds only a `PendingAttestation`: E907.
* `notes.ots` has no companion `notes` in the state, so it is ordinary
  content, and it has no proof `notes.ots.ots`: E902.
* If `extensions/NNNN-ots-timestamps/inventory.json.v1.ots` is missing: E904.

### Example 3: timestamping an existing object

An object has versions `v1` and `v2` without this extension. The client
adds the extension when it creates `v3`:

```json
{
  "extensionName": "NNNN-ots-timestamps",
  "timestampFrom": "v3"
}
```

`v1` and `v2` are not checked. The state of `v3` has a content proof for
every file, including the files carried forward from `v2`, and
`inventory.json.v3.ots` exists. If `timestampFrom` were later changed to
`v4`, the proof `inventory.json.v3.ots` would give W905.

## Implementation Notes

### Creating a Version

A client that creates version `vN` of an object with this extension:

1. Gets final content proofs for every new or changed content file (for
   the first covered version: for every file in its state), and adds each
   one to the version as `P.ots`.
2. Builds `vN/inventory.json`. Because the inventory lists the digests of
   the content proofs, it can only be built after those proofs are final.
3. Gets a final proof for the bytes of `vN/inventory.json` (if
   `timestampInventory` is `true`). This means a second wait for Bitcoin
   after step 1.
4. Writes the inventory proof into the extension directory **before** it
   updates the root `inventory.json`, so that the new version is never
   visible without its proof.

A client SHOULD NOT create the version while any required proof is pending.

### Files Named `.ots`

An archive can keep `.ots` files of its own. A standalone `x.ots` is
ordinary content and gets its own proof, `x.ots.ots`. If a user adds both a
file `P` and an existing proof `P.ots`, that `P.ots` is the content proof of
`P`: if it is a valid, final proof of `P`, a client MAY use it as is (it
keeps its original, earlier date); if not, the client SHOULD refuse it when
it is added and ask for one of the two files to be stored under another
path.

### Clients That Do Not Implement This Extension

A client that does not implement this extension can still read and validate
the object as a normal OCFL object. The `.ots` files are ordinary content,
and the extension directory is opaque, as the OCFL specification requires.
A new version created by such a client has no proofs, so the object then
fails the checks of this extension.

### Removing the Extension

The extension directory is not covered by any inventory. If it is deleted,
the inventory proofs are lost and this extension's checks no longer run,
although the content proofs remain in the content. Archives that depend on
this extension should check separately that the objects they expect to use
it still do.
