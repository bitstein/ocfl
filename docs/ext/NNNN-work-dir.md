# OCFL Local Extension NNNN: Work Directory

  * **Extension Name:** `NNNN-work-dir`
  * **Authors:** Michael Goldstein (Satoshi Nakamoto Institute)
  * **Minimum OCFL Version:** 1.0
  * **OCFL Community Extensions Version:** 1.0
  * **Related to:** [ADR-0001](../adr/0001-commit-claim-publish.md)

## Overview

This storage root extension holds the commits that are in progress. A
client assembles a new version here, outside every object, and then moves
it into the object in one step (the **claim**). The OCFL implementation
notes advise this: an object that is still being assembled stays where
validators ignore it (§3.7.1).

The directory holds nothing once every commit has finished. A client
removes `extensions/NNNN-work-dir/` when it is empty, so a storage root at
rest has no trace of the extension. While a commit is in progress, a
validator reports the directory as an unregistered extension (W016).

## Layout

```
[storage_root]
└── extensions/
    └── NNNN-work-dir/
        └── <sha256(object id)>/
            ├── plan.json
            └── build/
```

* `<sha256(object id)>`: the lowercase hex SHA-256 of the UTF-8 object ID.
  One commit per object can be in progress at a time.
* `plan.json`: the commit plan. It records only intent: the object ID, the
  new version, the digest of the base root inventory, and the new inventory
  bytes and digest, with an optional inventory proof. It records no
  progress. A recovering client reads the object to learn how far the
  commit got.
* `build/`: on a local filesystem, the complete new version directory
  (`vN/`), or the whole new object. It is moved into the object with an
  atomic rename that fails if the target exists. On an object store
  without renames, `build/` is not used.

The plan is written before anything else and removed after the root
sidecar is written. A `plan.json` with no live writer means a commit died;
the object must be recovered before it takes another commit or is purged.

## Parameters

None. There is no `config.json`.

## Name

The name is a placeholder. A permanent name is chosen with the
`NNNN-ots-timestamps` extension before launch (see
[docs/roadmap.md](../roadmap.md), "Before launch").
