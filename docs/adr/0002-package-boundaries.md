---
status: accepted
---

# ocfl-lib owns OCFL and the OTS extension rules; ocfl-cli owns workflows

`ocfl-lib` holds OCFL storage, validation, and the OTS extension's rules: the `.ots` parser, sidecar and config rules, the validation walk, and a pure attestation check against a `BlockHeaderSource` Protocol. A Bitcoin RPC client that implements the Protocol ships as the optional `[bitcoin]` extra (like `[s3]`), so readers such as sni-core can do chain checks. `ocfl-lib` has no network code in its base install. `ocfl-cli` owns everything that is a workflow: Staging Areas, Phases, batches, SimpleProof and ZMQ. It is layered as commands → use cases → adapters, and import-linter enforces the layers and the package direction.

## Considered Options

- **Staging in `ocfl-lib`** (ocfl-rs ADR 0001). Rejected: other consumers (sni-core, ocfl-index) only read and validate; staging is a CLI workflow with CLI-specific state (XDG dirs, source-file references).
- **A separate `ocfl-workflow` / `ocfl-ots` package.** Rejected: a package boundary only pays when a second consumer exists. The layers inside `ocfl-cli` give the same separation for less cost. Revisit if a second consumer appears.
