# CLAUDE.md -- job-kit plugin

Guidance for an AI agent working in this plugin. User-facing behavior is in
`README.md`; repo-wide rules for job-kit are the job-kit entries in
`plugins/CLAUDE.md`.

## Backpressure: wait, then retry as a new attempt

- A seam call that ends in a backpressure or rate_limit halt is recorded as its own attempt row with that halt; never loop on `backend.complete` under one attempt number -- see `plugins/CLAUDE.md` (the ledger's central claim).
- A `job-kit:backpressure-wait` event follows the attempt and marks it waited
  out. A waited-out attempt does not count toward `max_attempts`
  (`JobStore._reservation_budget_count`) and does not narrow dispatch (it adds
  nothing to the job's halted endpoints). The next call is a new reservation
  on the same model.
- When the next wait would pass the cap, that attempt is a plain `rate_limit`
  halt: it spends the budget and excludes the endpoint. Quota, credit and auth
  halts are never waited out.
- Code: `run_job` loops over `_run_job_attempt` with a `_WaitBudget`.

| Opinion | Default | Setting |
|---|---|---|
| Total wait per call | 600 s | env `JOB_KIT_BACKPRESSURE_CAP_S` (`0` disables), `BackpressurePolicy.cap_s` |
| First backoff without `retry_after_s` | 2 s, doubling | `BackpressurePolicy.base_s` (code only) |
| Backoff ceiling | 60 s | `BackpressurePolicy.max_s` (code only) |
| Jitter | +/-25% | `BackpressurePolicy.jitter` (code only) |

Razor verdict for base, ceiling and jitter: a deliberate code-level default;
a caller wanting another curve passes its own `BackpressurePolicy`.

## Storage

job-kit's new writes go only to its existing ledger (SQLite store); it adds no
second home for state.
