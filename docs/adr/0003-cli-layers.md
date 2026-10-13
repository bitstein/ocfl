---
status: accepted
---

# ocfl-cli is layered commands → use cases → adapters; import-linter enforces it

ADR-0002 put workflows in `ocfl-cli` and asked for layers inside it. Before this ADR, `stage/commands.py` (4830 lines) parsed options, ran the workflows, held the batch engine and wrote files. `stage/service.py` (2318 lines) mixed use cases with persistence. We split the CLI by what each piece does:

| Layer | Packages | Holds |
|---|---|---|
| composition root | `ocfl_cli.main` | the Typer app; wires the production adapters into `AppCtx` |
| commands | `ocfl_cli.archive`, `ocfl_cli.stage`, `_appctx`, `_common`, `_exit`, `_style`, `exit_codes` | option parsing, rendering, the one error handler |
| use cases | `ocfl_cli.workflow` | the Staging Area workflows: `staging`, `status`, `content`, `inventory`, `commit`, `watch`, `validation`, the batch engine (`batch`) and shared helpers (`common`) |
| adapters | `ocfl_cli.ots`, `ocfl_cli.store` | SimpleProof HTTP, Bitcoin RPC/ZMQ (`ots`); staging files, `ots.json`, batch files, locks (`store`) |
| system | `ocfl_cli.system` | the real clock and sleep behind the time ports |
| base | `ocfl_cli.model`, `ocfl_cli.ports`, `ocfl_cli.config`, `ocfl_cli.errors` | value types, the Protocols the use cases need, configuration, errors |

`ocfl_lib` is a library under all of them; any layer may use it.

Rules, enforced by import-linter (`[tool.importlinter]` in the root `pyproject.toml`, run by `uv run lint-imports` and by `packages/ocfl-cli/tests/test_cli_architecture.py`):
- A layer imports only the layers below it (`layers` contract, `exhaustive`, so a new top-level module must be placed).
- Commands do not import adapters, not even for types; they call use cases, which return models (`forbidden` contract).
- `ocfl_lib` never imports `ocfl_cli`.
- A `_name` is private to its module, and a `_module` to its package: no module imports another module's `_name` (checked by an AST test; import-linter cannot see names).

## Considered Options

- **Keep the packages by feature (`stage`, `ots`, `archive`) and add contracts per module.** Rejected: `stage` mixed all three layers, so the contracts would need a list of exceptions instead of a rule.
- **One package per layer (`commands/`, `usecases/`, `adapters/`).** Rejected for now: `archive/validate.py`, `ots/bitcoin.py` and `_common.py` are pinned by the phase-4 guards, and the feature names read well inside each layer. The layer of each package is stated once, in the contract.

## Consequences

- Moving a function across a layer is a contract change that import-linter reports.
- No contract lists exemptions (`ignore_imports`).
- The use cases reach time only through the ports (`Clock`, `Sleeper`, `BlockStream`, bundled with the lock timeout as `ports.Runtime`), and commands open the storage root through the `fs_factory` port; `ocfl_cli.system` is the one module that reads the real clock or sleeps. Tests inject fakes through `ctx.obj` / `AppCtx` or a `Runtime`, not by patching module internals.
