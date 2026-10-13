---
status: accepted
---

# Every CLI lock is a flock with a 5 s wait; the batch flock is the lease

Before this ADR the CLI had two lock mechanisms. `filelock` guarded a Staging Area (`<staging>/.lock`), a batch file (`batches/<id>.lock`, held only around each write) and a commit (`$TMPDIR/ocfl-cli-object-locks/<key>.lock`), each with a 30 s wait. Because the batch lock was dropped between writes, a second mechanism kept two drivers off one batch: the driver lease, a `batches/<id>.driver` JSON file (pid, host, start, heartbeat, command, CLI version) with a 60 s heartbeat thread, a `.driver.refresh-lock`, stealing after 300 s without a heartbeat or when the pid was dead on this host (renamed to `.driver.stolen.<pid>.<ts>`), and SIGINT/SIGTERM handlers plus an `atexit` hook to delete the file. A file lease needs all of that because the file outlives a holder that crashes.

## Decision

- One primitive, `store.locks.flock(path, runtime, what=...)`: an exclusive `flock(2)` on a lock file, tried with `LOCK_NB` and retried every 0.1 s through `Runtime.sleeper` for `Runtime.lock_timeout` (default 5 s), then `LockTimeoutError` (BUSY, exit 4). The Staging Area lock, the batch lock and the commit lock all use it.
- The batch lock is held for a whole batch run: `stage batch create|submit|upgrade [--watch]|retry|commit|rm`. It is the lease: a second driver, a commit or an `rm` of the same batch waits 5 s, then exits 4. `stage batch status|ls` take no lock (the batch file is replaced atomically). Inside a run the engine takes no further batch lock: a second `flock` on the same file from the same process would block itself.
- The `.driver` lease, its heartbeat thread, the refresh lock, stealing, the signal handlers and `DriverLease` are removed, and so is the `filelock` dependency.
- One lock order: batch, then commit, then Staging Area. Nothing takes a batch lock while it holds a Staging Area lock.
- Lock files stay after a run; deleting one could let a process that already opened it and a new one both hold "the" lock. `stage batch rm` is the one exception: it deletes the batch file and then its lock file while holding the lock, so a waiter that gets the old lock finds no batch and stops.

The kernel releases a `flock` when its holder exits, however it exits (crash, `SIGKILL`, OOM). So a lock can never go stale, and there is nothing to heartbeat, steal or compare clocks for. The wait goes through the `Sleeper` port (ADR-0003), so tests wait no real time.

## Considered Options

- **Keep the `.driver` lease.** Rejected: it rebuilds, with a thread, signal handlers and clock comparisons, what a held `flock` gives for free, and it could still be stolen from a live holder whose heartbeat stalled.
- **POSIX record locks (`fcntl.lockf`).** Rejected: they belong to the process, not the open file, and any `close()` of that file in the process drops them.
- **A blocking `flock` cut short by `SIGALRM`.** Rejected: signals work only on the main thread, and the wait would bypass the `Sleeper` port.
- **Keep the 30 s wait.** Rejected: exit 4 already means "busy, retry"; scripts and operators learn that faster after 5 s.

## Consequences

- The staging root and `$TMPDIR` must be on a local filesystem: `flock` over NFS is emulated or unsupported. The staging root is a per-user XDG state directory, so this holds by default. Two machines sharing a staging root were never reliably kept apart (the lease trusted host and pid).
- A `stage batch upgrade --watch` holds its batch for hours, so `stage batch commit` of that batch exits 4 until the watch ends; batch commit runs after the watch, not beside it.
- The batch file format is unchanged; old `.driver*` files in a staging root are ignored and can be deleted.
