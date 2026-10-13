---
status: accepted
---

# Exit 75 means "still pending": an upgrade pass ran and the proofs are not final yet

A proof becomes final only after a Bitcoin block, so a one-shot `stage upgrade` usually finds it still pending. Until now it exited 0 whether the proofs were final or not. A script or cron job had to parse `--json` (`done`) to know whether to run again. ocfl-rs SPEC §6.3 (D19) already uses 75 for this.

## Decision

- New exit code 75 (`EX_TEMPFAIL` in `sysexits.h`: "try again later"). It is a result, not an error: no `ErrorClass` maps to it, nothing is printed on stderr, and under `--json` the result document is printed as usual.
- Only two commands use it, and only without `--watch`:
  - `stage upgrade OBJ`: the pass ran and the target's proofs are not all final.
  - `stage batch upgrade BATCH`: the pass ran and a row still waits for its proofs (`submitted`).
- Everything else keeps its code. `stage timestamp` exits 0 after a successful submission, and so does a verb with nothing to do (it names the next command). `--watch` exits 0 once the proofs are final (130 when interrupted).
- The exit codes are 0, 1, 2, 3, 4, 5, 6, 75, 130.

A cron job can now loop on the exit code alone: 0 final, 75 run again later, anything else an error.

## Considered Options

- **75 for every result that is not final yet, `stage timestamp` included (ocfl-rs SPEC §6.3).** Rejected: a submit that succeeded would exit non-zero, so `stage timestamp X && ...` and `set -e` scripts stop on the normal path.
- **Keep 0 and rely on `--json` `done`.** Rejected: every caller needs a JSON parser to tell "done" from "come back later", and the two CLIs would differ.
- **Exit 3 (state error) while pending.** Rejected: pending is the expected state, not a wrong one.

## Consequences

- Scripts that ran `stage upgrade` (or `stage batch upgrade`) once and treated any non-zero exit as an error must accept 75.
- The exit-code guards and help list 75.
