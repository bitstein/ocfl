# Changelog

All notable changes to `ocfl-cli`. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
No release is published yet, so every entry below is unreleased and may still change.

## [Unreleased]

### Layers (ADR-0003)

- `ocfl_cli` is layered commands → use cases → adapters, and import-linter enforces it (`uv run lint-imports`; contracts in the root `pyproject.toml`). Commands: `main`, `archive`, `stage`; use cases: `workflow`; adapters: `ots`, `store`; base: `model`, `ports`, `config`, `errors`. No behavior or output change.
- Modules moved: `stage.service` and `stage.watch` → `workflow.{staging,status,content,inventory,commit,common,watch}`; `stage.commands` → `stage.{_app,edit,status,commit,timestamp,batch,render}`; `stage.{store,state,_wire,batch,ingest}` → `store.{staging,ots_state,_wire,batch,ingest}` with their value types in `model.{ots_state,batch,content}`; `stage.{fsm,schemas}` → `model.{phase,metadata}`; `config`, `archive.config`, `ots.config` → `config`, `config.archive`, `config.ots`; the `BitcoinRPC`, `HashBlockStream` and `SimpleProofClient` Protocols and the client factory types → `ports`; SimpleProof result types → `model.simpleproof`.
- The batch engine moved from `stage.batch` into `workflow.batch` (`drive`, `prepare`, `submit`, `retry`, `upgrade`, `watch`, `report`, `reasons`). Its drivers report through a `BatchEvents` Protocol that the command renders; batch files, leases, events and output are unchanged.
- Ports for time (`ocfl_cli.ports`): `Clock`, `Sleeper` and `BlockStream` (ZMQ `hashblock`), with `ocfl_cli.system` as the only module that reads the real clock or sleeps. `AppCtx` carries `clock`, `sleeper`, `block_stream_factory` and `lock_timeout`, and the use cases receive them as one `ports.Runtime`. Tests inject fakes under the same `ctx.obj` keys instead of patching module internals. The SimpleProof client port states its `submit_chunk_size`; batch `drive.commit` takes the per-object commit as a `commit_object` dependency. No output or format change: batch files, `ots.json` and lease timestamps come from the injected clock.
- `AppCtx.fs_factory` (port `ports.FsFactory`, default the lib's `open_fs`) opens the storage root's filesystem for every command, so tests inject a faulting filesystem instead of patching the commit's content source.
- Helpers used across modules lost their leading `_` (e.g. `object_commit_lock`, `upgrade_content`, `SUBMIT_CHUNK_SIZE`); no module imports another's `_name`.

### Output

- Each command run makes its own stdout and stderr consoles (`AppCtx.console`, `AppCtx.err_console`), coloured from that run's `--no-color`, `NO_COLOR`, `FORCE_COLOR` and TTY. `_style` keeps no consoles: `--no-color` no longer leaks into later runs in the same process, and `FORCE_COLOR`/`NO_COLOR` are read per run instead of at import. Removed `_style.echo`, `set_no_color` and `reinit_from_env`; use `_style.make_console`.

### Errors and exit codes (docs/roadmap.md §5)

- One error handler at the root command group. Commands raise; the handler prints the error and exits. Every CLI error carries an `ErrorClass` and a stable snake_case `code`, like the lib's errors, and `exit_codes.exit_code_for_class` maps the class to the exit code with one exhaustive `match` (ocfl-rs ADR 0004): `NOT_FOUND`, `INVALID`, `SERVICE` → 1; `USAGE` → 2; `STATE` → 3; `BUSY` → 4; `HALT` → 5; `CONFIG` → 6; `INTERRUPTED` → 130. The documented exit codes are unchanged.
- Under `--json`, an error is one JSON line on stderr, `{"error": {"code", "message", "exit"}}`, and stdout stays empty. Text mode still prints `Error: <message>` on stderr.
- `ocfl validate --json` with an unreachable Bitcoin node now reports the failure as a JSON error on stderr (`code` from the lib error, e.g. `bitcoin_rpc_transient`); it still exits 1 and never reports E906 for a node failure. Text mode still prints `Error: cannot verify OTS attestations: …`.
- `validate --json` reports a root or object it cannot open as a JSON error on stderr instead of `{"valid": false, "errors": [<message>]}` on stdout.
- `stage timestamp batch … --batch ID` with no batch file: under `--json` the error is the stderr JSON error (`code` `batch_file_missing`, exit 2) instead of `{"batch_id", "reason", "next"}` on stdout.
- Lib errors that reach the CLI now exit with their class's code instead of 1: e.g. `init` on an existing root (`root_exists`, STATE) exits 3, a busy object lock (`object_locked`, BUSY) exits 4, `recover` with an action that does not apply (`recovery_action`, USAGE) exits 2, a bad layout config or unsupported backend (CONFIG) exits 6, and a Bitcoin RPC auth failure (`bitcoin_rpc_auth`, CONFIG) exits 6.
- Errors that used to escape as a Python traceback with exit 1 now go through the handler too: e.g. a held batch driver lease (`lock_timeout`) exits 4, an `OSError` exits 1 with `Error: …`, and an uncaught Ctrl-C exits 130 instead of click's `Aborted!` with 1.
- The CLI uses the lib's Bitcoin RPC client and errors (`ocfl_lib.bitcoin.BitcoinRpcClient`, `BitcoinRpcError` and subclasses) directly. Removed `BitcoinRPCClient` and the CLI's `BitcoinError`, `BitcoinAuthError`, `BitcoinRPCTransient`, `BitcoinBlockMissingError` and `BitcoinUnavailable`.
- New errors: `UsageError` (exit 2), `ErrorContext` (adds context to another error's message and keeps its class and code), `BatchFileMissingError`, and `CommitFailedError` subclasses `CommitClaimLostError` (exit 3) and `CommitInterruptedError` (exit 130), which replace its `claim_lost` / `interrupted` flags.
