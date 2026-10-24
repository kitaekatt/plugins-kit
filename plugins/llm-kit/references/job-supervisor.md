# Supervised local jobs

`llm_kit.jobs.supervisor` owns one local job process across manager restarts.
It requires `katsoft-llm-kit[local]` and first ships in package version 1.6.
It does not change the jobs ledger schema or the existing jobs commands.

## Start and inspect

Use an absolute, private run directory for each attempt.
Use the same `lease_path` for all supervised writers of one ledger.
The library refuses a second writer until the prior lease releases.

```python
from pathlib import Path
import sys

from llm_kit.jobs.supervisor import JobProcessSpec, start, inspect, adopt, cancel

root = Path.cwd().resolve()
store = root / "private-runs" / "jobs.sqlite3"
handle = start(JobProcessSpec(
    run_dir=root / "private-runs" / "attempt-1",
    cwd=root,
    argv=(sys.executable, "-m", "llm_kit.cli", "jobs", "run",
          str(root / "jobs.yaml"), "--store", str(store), "--run-id", "attempt-1"),
    timeout_s=7200,
    cancel_argv=(sys.executable, "-m", "llm_kit.cli", "jobs", "cancel", "attempt-1",
                 "--store", str(store), "--reason", "supervised process stopped"),
    lease_path=root / "private-runs" / "ledger.lease.json",
))
saved_handle = handle.to_dict()
status = inspect(handle)
print(status.to_dict())
```

`JobProcessSpec` is immutable. It accepts `env` overrides, `grace_s` and `cancel_timeout_s`.
The default termination grace is 2 seconds. The ledger cancellation deadline is 30 seconds.
The specification copies the environment overrides and freezes them.
The run files contain only those explicit overrides, not the full inherited environment.

`start` persists the specification and lease before it starts the detached supervisor.
The child records its identity before the supervisor authorizes the command.
`start` returns after that handshake, or after a terminal refusal.
A startup timeout requests cancellation and raises `SupervisorError` with a `handle` attribute.
Inspect or adopt that handle before another attempt.

`inspect` reads durable state and checks live process identities. It makes no ledger writes.
The result contains `state`, `alive`, `returncode`, `detail`, `survivors` and the final `result` record.
`stdout_path` and `stderr_path` name the command logs.
`to_dict()` returns schema-1 JSON-compatible state.
`ProcessHandle.from_dict(saved_handle)` restores the run directory and its nonce.
A wrong nonce refuses control before any cancellation request or signal.

## Cancel and recover

```python
status = cancel(handle, "operator cancelled", wait_s=10)
status = adopt(handle)
```

`cancel` records a request. The supervisor stops descendants before it finalizes the ledger.
PID and creation time must both match before any signal.
The finalizer uses its own durable child handshake and process identity.
Its command must be idempotent. The existing `jobs cancel` command satisfies that requirement.
Its observed return code, stdout and stderr appear under `result.cancel`.
No cancellation command is necessary for a command that does not own a jobs ledger.

`adopt` retains a live owner. It never starts another copy of the job command.
After owner loss, a detached recovery supervisor stops recorded descendants and finalizes the ledger.
A clean recovery reports `cancelled`; its detail states that prior work can be partial.
This is not an automatic job retry or resume.

The states are `running`, `cancelling`, `finished`, `cancelled`, `timeout`, `refused` and `indeterminate`.
`finished` means that the command exited. Its return code and acceptance evidence need separate inspection.
`refused` means that no command was authorized and no owned process remains.
Survivors, uncertain ownership and failed ledger cancellation report `indeterminate` and retain the lease.
An active supervisor can still have `alive: true` briefly after it writes a final record.
Wait for `alive: false` before retry.
`cancel` and `adopt` return the current status when their bounded wait ends.

The library never interprets task state, model consent, harness permissions or task acceptance.
The manager owns those policies and must inspect them before dispatch.
An ordinary jobs CLI invocation does not acquire the supervisor's lease.
All writers of the shared resource must use this wrapper for its exclusion guarantee.

## Platform boundary

On POSIX, recovery finds recorded descendants and members of the command's original session.
A process that changes session and reparents before observation can escape this boundary.
On Windows, the supervisor joins a kill-on-close Job Object before dispatch.
Dispatch refuses if Windows cannot assign that job.
Recovery uses another detached supervisor with the same containment requirement.
The process wrapper does not provide a security sandbox against hostile code.

Run files belong in a private, machine-local directory.
They contain the caller's argv and explicit environment overrides.
Do not share them through a repository or expose them through an unauthenticated route.
