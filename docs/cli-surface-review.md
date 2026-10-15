# CLI surface review (roadmap §7.4)

The 37 issues of `ocfl-rs/docs/port/python-cli-surface.md` §6, one decision each. An issue is taken only when it is justified on its own; the `(7.n)` tag names the roadmap item whose commit carries it, and `(follow-up)` a change made after §7. Exit codes are 0, 1, 2, 3, 4, 5, 6, 75 (ADR-0006), 130. Terms: [CONTEXT.md](../CONTEXT.md).

| # | Issue | Decision | Why |
|---|---|---|---|
| 1 | `--watch` requires `--upgrade` on one object but excludes it on a batch | adopt (7.1) | The modes are verbs now; `--watch` is an option of `timestamp`, `upgrade`, `batch create` and `batch upgrade` and means one thing: do the verb, then upgrade on each block until the proofs are final. |
| 2 | `--watch` means different things; the single-object loop has no first pass; the batch loop also polls on each 5 s ZMQ timeout | adopt (7.1, 7.4) | One meaning through the verbs (7.1). The single-object loop runs one pass before the first block, so proofs that are already final do not wait about ten minutes (7.4). The batch loop's poll on a timeout stays: SimpleProof finishes jobs between blocks, and dropping it would only slow batches down. |
| 3 | One command with four exclusive modes | adopt (7.1) | Each mode is a verb, so the flag-combination checks are gone. |
| 4 | `--retry` means two things | adopt (7.1) | `stage retry` and `stage batch retry` resend failed submissions; automatic transient retries stay internal. |
| 5 | The target is redundant for upgrade and retry | adopt (7.1) | The Timestamp Target comes from the Phase; `--expect content\|inventory` keeps an explicit assertion (exit 3 on a mismatch). |
| 6 | `stage timestamp batch content --batch ID --upgrade` is five levels deep with "batch" twice | adopt (7.1) | `stage batch upgrade ID`. |
| 7 | The batch lifecycle is split across three places | adopt (7.1) | `stage batch create\|submit\|upgrade\|retry\|commit\|status\|ls\|rm`. |
| 8 | No content-batch to inventory-batch handoff | adopt (7.4) | `stage batch create inventory --from C_ID` takes the content batch's upgraded rows; typing the list again was error-prone. |
| 9 | `next` hints are not copy-pasteable | adopt (7.1) | One module (`workflow.hints`) builds every hint as a full `ocfl stage ...` command with the object or batch id. Global options are left out: the hint runs in the same environment. |
| 10 | Short batch ids are shown but not accepted | adopt (7.4) | A unique prefix names a batch, like a git short hash; an ambiguous one is exit 2, an unknown one exit 1. |
| 11 | A never-staged object is reported as `committed` | adopt (7.4) | New skip reasons `not_staged` and `not_ots`; `batch create`/`submit` then exit 1 and name the objects, so a typo no longer looks like success. |
| 12 | `batch rm` leaves `.lock`, `.driver` and `.refresh-lock` behind | adopt (7.3) | The `.driver` files are gone with the lease; `rm` deletes the lock file while it holds it (ADR-0005). |
| 13 | `--force` has three meanings | adopt (7.4) | `purge -y/--yes` skips the prompt; elsewhere `--force` overrides a safety check (`discard`, `batch rm`). |
| 14 | Short-only flags (`log -n`, `stage rm -r`) | adopt (7.4) | `log -n/--limit`, `stage rm -R/--recursive`; `-r` is dropped because it clashed with the global `-r/--root`. |
| 15 | `-V/--version` means the object version, and the program has no version flag | adopt (7.4) | `ls` and `export` take `-V/--object-version`; `ocfl --version` prints the program version (33). `show OBJ [VERSION]` stays positional, like git. |
| 16 | `validate` flag styles clash; default-on `--verify-ots` needs a node | adopt (follow-up) | `--ots=auto\|local\|chain`, default `auto`: chain checks when `[bitcoin]` is configured, else local checks and a note. No `off` mode: the extension's own checks always run, so a validator never reports less. |
| 17 | `--digest` offers algorithms it then refuses | adopt (7.4) | The choices are sha512 and sha256, the content digests OCFL 1.1 allows and the CLI accepts. |
| 18 | `reset` is a misleading name; nothing staged exits 0 | adopt (7.2) | `stage discard`. Nothing staged still exits 0 with a note, so it is safe to run twice. |
| 19 | `stage ls --json` is shaped unlike `ocfl ls --json` | adopt (7.4) | Always `[{"path"[, "digest"]}]`. |
| 20 | `batch status --verbose` shadows the global `-v/--verbose` | adopt (7.1) | `--detail`. |
| 21 | `--staging-root` is a group option | reject | Only `stage` commands use it, and `OCFL_STAGING_ROOT` covers scripts; a second, global spelling buys little. `config path` shows the effective staging root. |
| 22 | Config and state directories are named differently | reject | Renaming the state directory would strand in-flight Staging Areas for a cosmetic gain; `config path` prints both. |
| 23 | `--json` is ignored by single-object timestamping and watch | adopt (7.1) | A one-shot verb prints one document; `--watch` prints NDJSON events ending in a `summary` event. |
| 24 | Batch lock contention exits 1 with a traceback | adopt (phase 5, kept in 7.3) | Exit 4 since phase 5; the flock lease keeps it 4. |
| 25 | Exit 5 also covers a missing or unverified proof | adopt (7.4) | At commit, a missing, unverified or uncached inventory proof, or a stale plan, is a state error (exit 3, `inventory_proof_missing`). Exit 5 stays for a cached proof that does not parse or commits to other bytes, and its help says "operator review" instead of "retry after re-fetch". |
| 26 | Exit 2 is used for not found | adopt (7.4) | A missing batch is exit 1 (`batch_file_missing`); a commit of a content batch or of another root's batch is exit 3 (`batch_mismatch`). `ocfl-index` is unchanged (35). |
| 27 | `upgrade` gives scripts no "still pending" exit | adopt (follow-up) | ADR-0006: a one-shot `stage upgrade` / `stage batch upgrade` exits 75 while proofs are pending, so a cron job loops on the exit code alone. `stage timestamp` keeps 0 after a submit, so `&&` chains and `set -e` scripts still work. |
| 28 | Mutators print nothing | reject | The exit status is the acknowledgement for `new`, `add`, `mv`, `rm`, `metadata` and `commit`, as in other Unix tools; scripts that need state read `stage status --json`. Verbs that move the Phase print it (7.1). |
| 29 | No way to list Staging Areas | adopt (7.4) | `stage ls` with no object lists the storage root's Staging Areas and their Phase. |
| 30 | No one-shot pipeline (`seal`) | reject | `stage timestamp OBJ --watch` twice, then `stage commit`, is three commands with no flag combinations; one command over a multi-hour run with two waits adds little. Can come later (ocfl-rs SPEC D17). |
| 31 | No config inspection or connectivity check | adopt (7.2) | `ocfl config path\|show\|check`. |
| 32 | A broken `[simpleproof]`/`[bitcoin]` section blocks every command | adopt (7.2, 7.4) | Such a section fails only the commands that need it (7.4); `config path` and `config check` run even when the config file does not load (7.2). |
| 33 | No `ocfl --version` | adopt (7.4) | Root `--version`. |
| 34 | `validate` has no root-level OTS walk and no content-proof chain walk | adopt (follow-up) | Root-level `validate` checks each object's proofs against Bitcoin (`validate_root(chain=...)`). What the chain check covers is unchanged: the same as object-level `validate`. |
| 35 | `ocfl-index` is a separate binary | reject | It is its own package with its own consumers (ADR-0002); folding it into `ocfl` is a packaging decision for the index work, not part of this review. |
| 36 | `export --to -` silently writes only the first match | adopt (7.4) | More than one match is a usage error (exit 2). |
| 37 | `stage commit` takes one object | reject | `stage batch commit` commits many; committing several unrelated objects in one run adds partial-failure rules for what a shell loop already does, and a commit stays a deliberate step (ocfl-rs SPEC D17). |
