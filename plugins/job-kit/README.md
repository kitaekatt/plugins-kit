# job-kit

Durable execution for heterogeneous agent jobs through a bounded worker pool.

A job file declares work; the runner executes each job once, selects a model
from the job's declaration through llm-scripting-kit, and accepts a result only
when a command says so.

```bash
job-kit run jobs.yaml [--store PATH] [--timeout SECONDS] [--run-id ID] [--max-parallel N]
job-kit status <run-id> [--store PATH]
job-kit resume <run-id> [--store PATH] [--timeout SECONDS] [--max-parallel N]
job-kit resolve <run-id> <interrupt-id> (--input JSON | --input-file PATH | --reject [--reason TEXT]) [--store PATH]
job-kit events <run-id> [--store PATH] [--out PATH]
job-kit gc [<run-id>] [--store PATH] [--accepted-only] [--force]
```

`--run-id` preassigns the run's identity, so a caller that backgrounds a run
already knows what to pass to `status` and `resume`. Without it the runner
generates one and prints it with the final snapshot -- which is too late to
poll. `--timeout` is job-kit's own per-attempt budget and defaults to 900
seconds.

Exit codes: **0** every job accepted (or the verb succeeded), **1** a job was
rejected, failed, halted, could not be routed, was rejected by an operator or
expired (or `resolve` refused a resolution), **2** a usage error, **3** the
runner itself failed (including an unknown run or interrupt), or `events`
refused an export, **4** `run` or `resume` ended with no failure but at least
one job waiting on an interrupt. An unattended caller should branch on 1 versus
3: the first is a result about the work, the second is a result about job-kit.
Exit 4 is neither: the run is healthy and waiting on a person (see "Durable
interrupts").

## The job file

```yaml
jobs:
  - id: lint
    prompt:
      system: "You are a coding assistant."
      user: "Fix the lint errors."
    models: [qwen38-5090, luna, sonnet]
    requirements:
      params: [cwd]
    directory: .
    contract:
      command: [python, -m, pytest, tests/lint]
```

## What it gives you

- **Deterministic selection from a model declaration.** `models` is a list of
  llm-scripting-kit registry ids in the one declaration format (bootstrap's
  plugin-dev skill, `references/model-declaration.md`); a scalar is a
  one-element list. Each attempt calls llm-scripting-kit's
  `describe(caller="process")` over it and takes the FIRST USABLE entry of the
  pace-ordered list. An entry is skipped, silently, when it does not resolve,
  does not advertise the `requirements` the job states, is out of quota, is
  probed unreachable, or is excluded by a halt. Paced entries (those declaring
  `conserve_usage`) are ordered by pace, highest first; unpaced entries keep
  their declared places. Every attempt records the pace readings it was
  selected from (`pace_readings` in `job-kit status`), so a run is explainable
  from the declared list plus those readings. When no declared entry is usable
  the job ends with the floor: an error naming every declared id and why it
  could not run, which is the one place a skipped id is ever named. The
  `requirements` mapping is llm-scripting-kit's requirement language over an
  adapter's advertised `Capabilities` -- see llm-scripting-kit's README,
  "Capability requirements" subsection; an entry whose backend advertises
  nothing is never selected. The keys `endpoint_preference`, `endpoint_preferences`, `endpoints` and
  `endpoint` are rejected: a job that uses one of them in place of `models`
  fails loading with an error that names the key and points to `models`.
  Requires llm-scripting-kit >= 0.56.0, the version that added
  `completion.json_schema` with its frozen subset marker
  (`SUPPORTED_SUBSETS`), which `run`, `resume` and `resolve` probe before
  they open the ledger (exit 3 without it). Selection needs
  llm-scripting-kit >= 0.46.0, the
  version that added `describe` (and, before it,
  `subjects_for_disallowed_tools` for the deny floor); job_kit.select fails at
  import time with a named remediation if an older llm-scripting-kit is
  linked in.
- **Command-shaped acceptance.** A `contract` command must exit zero for the
  attempt to be accepted. Model output that does not satisfy it is a failure,
  not a result, so nothing downstream has to trust the text.
- **Durability.** Runs are recorded. `status` reports one, `resume` continues
  the non-terminal jobs of one, and an interrupted run does not restart from
  the beginning. A write-ahead reservation records the invocation boundary
  before the seam. Every attempt is still exactly one observed seam invocation:
  a process loss after arming is recorded as a reservation loss, not as a
  fabricated attempt row, and it consumes one retry budget unit.
- **Halts narrow the run; timeouts do not.** An endpoint that returns a
  persistent halt (auth, rate limit, insufficient credit, or a spent
  subscription pool) is excluded from the rest of the run, and the job's next
  attempt re-selects from what remains. A quota or credit halt on an entry that
  declares `conserve_usage` also records that entry out of quota until its
  reset, so later selections in the session skip it too. `max_attempts` bounds
  executions only: a halt that spends the last attempt ends the job with
  "attempt limit reached", never with the floor. An unreachable endpoint
  is excluded from the run only after a confirming probe: two observed
  unreachable attempts on that endpoint, where the later attempt starts after
  the earlier attempt ends and no non-unreachable attempt starts between them.
  An unreachable endpoint is excluded from later attempts of the same job after
  its first unreachable result. A `--timeout` expiry is job-kit's own budget
  rather than evidence about the endpoint, so it is recorded as a retryable
  timeout and the endpoint stays eligible.
- **Per-attempt worktrees are opt-in.** A job in a git repository that sets
  `workspace.isolate: true` runs each attempt in a detached worktree at the
  run's observed HEAD. A job without that setting runs in its declared
  directory. A failed attempt's worktree survives until garbage collection.
  `job-kit gc <run-id> [--store PATH]` reclaims it; a dirty worktree needs
  `--force`.
- **A tool deny floor.** The job-file-level `disallowed_tools` applies to every
  job in the run. Harness endpoints are agent sessions, not plain completions;
  the floor is how a run declares what they may not do.

When `--store` is omitted, the default store path is CWD-relative:
`.local-data/plugins-kit/job-kit/runs.sqlite3` under the current working
directory.

## Options

A job's optional `options` mapping carries `allowed_tools`,
`disallowed_tools`, `effort`, `system_prompt_mode`, `max_tokens`, `temperature`
and `extras` through to the completion seam. `max_tokens` must be at least 1
and `temperature` must be in the range 0 to 2. If omitted, they default to 4096
and temperature is unset, so the endpoint/model default applies; set it per job
to override. `effort` overrides the reasoning effort the endpoint registry entry
carries: effort is a property of the ENDPOINT, so a job that needs more
deliberation than its endpoint's default says so here, and leaving it unset
emits exactly the argv an existing job file always did.

`workspace` accepts `directory`, `base_ref` and `isolate`; `isolate` defaults
to `false`.

Every path-typed field -- `directory`, `contract.directory`,
`workspace.directory`, `workspace_root` -- resolves relative to the job file's
own directory, so `directory: .` means the job file's directory and job files
need no absolute paths. Paths inside PROMPT text are just text and are not
resolved.

### What a contract receives

The contract command runs in the attempt's workspace with the completion text
on **stdin** and this attempt's identity in the environment: `JOB_KIT_RUN_ID`,
`JOB_KIT_JOB_ID`, `JOB_KIT_ATTEMPT_NO`, `JOB_KIT_ENDPOINT`, `JOB_KIT_BACKEND`,
`JOB_KIT_MODEL`. Three more concern interrupts and are described under
"Durable interrupts": `JOB_KIT_INTERRUPT_REQUEST` (where a contract may write a
request), and, on a continuation only, `JOB_KIT_INTERRUPT_ID` and
`JOB_KIT_INTERRUPT_RESOLUTION`. `JOB_KIT_CONTINUATION_NO` accompanies them as a
counter.

Use them. A contract that only checks a fixed output path for an existing file
passes on an artifact an EARLIER run left behind -- an attempt that produced
nothing at all is accepted. Read the result from stdin, or write it to a path
carrying `$JOB_KIT_RUN_ID`, so the check observes THIS attempt.

Usage counts are nullable: a transport can complete without reporting tokens,
and unknown usage is recorded as unknown rather than as zero.

## Making one job depend on an earlier one

A run is a FLAT set: job-kit passes nothing between jobs and gives each its own
workspace. A later job can still consume an earlier one's output, through two
facts and no plugin feature:

- the earlier job's contract writes its result somewhere OUTSIDE the workspaces
  (an absolute path, or a directory named by an environment variable you set
  before `run`), because garbage collection can discard a worktree;
- jobs run in declaration order, so the earlier job is finished before the later
  one starts.

**Put the correlation in the contract, not the prompt.** A prompt is an opaque
string and cannot interpolate the run id, so a prompt can only say "the newest
file matching this pattern" -- which will happily read a PREVIOUS run's output.
The downstream contract has `JOB_KIT_RUN_ID`, so it can require the upstream
artifact of THIS run and reject anything else. Loose prompt, strict contract.

**This holds only at `max_parallel: 1`** (the default). Above that a flat set
gives no ordering guarantee, so a file-mediated dependency is not safe. A DAG is
deliberately out of scope; if you need ordering beyond what declaration order
gives you, run two runs.

## What `max_parallel: N` gives up

`max_parallel: N` runs up to N JOBS at once, each in its own worker. The unit is
a whole job driven to a terminal state or to its attempt budget, so a single
job's attempts stay strictly sequential and the attempt ledger stays
append-only. `job-kit run --max-parallel N` overrides the file and is what the
ledger records; `job-kit resume --max-parallel N` applies to that pass only and
does not rewrite the record.

Four properties are forfeited above 1, and none of them is recoverable by
configuration:

- **Ordering.** Jobs are submitted in declaration order and finish in whatever
  order they finish. The file-mediated dependency above is unsafe here.
- **Halt narrowing becomes dispatch-time only.** An endpoint that returned a
  persistent halt is excluded from jobs dispatched AFTER the halt is recorded.
  Jobs already in flight on that endpoint are never cancelled: they run to
  completion and their attempt rows are recorded truthfully, because an aborted
  invocation cannot be reported honestly.
- **Ctrl-C stops dispatch, not work.** An interrupt stops new dispatch and waits
  for in-flight attempts to finish. The interrupt-recording paths in the runner
  fire in the thread that owns the attempt, so at `max_parallel: 1` an interrupt
  is recorded against the attempt it hit; above 1 the run ends after the
  in-flight attempts complete and are recorded as their own outcome.
- **Interleaved stderr.** Backends stream to stderr as they go, so N workers
  produce interleaved output. Each line carries its job through the
  `[job:<id>]` prefix; the ledger, not the console, is the record of a run.

## Execution events

Every ledger transition also records an execution event in the common
envelope `plugins-kit.execution-event/v1` (bootstrap's plugin-dev skill,
`references/execution-events.md`). The event is written in the same SQLite
transaction as the fact it describes, so it exists if and only if the fact
committed. `job-kit events <run-id>` prints the run's stream as JSON Lines, in
`seq` order; `--out PATH` writes it to a new file instead and refuses a file
that already exists.

| Ledger fact | Event |
| --- | --- |
| run created | `job-kit:run-created` (payload `max_parallel`, `job_count`) |
| attempt reserved | `dispatch-selected` (payload `endpoint`, `budget_no`) |
| invocation armed | `call-started` |
| attempt appended | `usage` when usage is known; `result` (`status`, and `error_code`, `halt_kind`, `acceptance` when present); `terminal` when the attempt ends the job |
| reservation resolved before invocation | `result` (`status: not-invoked`, `reason`), then `terminal` (`state: failed`) |
| reservation lost to a dead process | `result` (`status: lost`, `reason`), then `terminal` when the loss spends the last attempt |
| job marked unroutable, halted or failed | `terminal` (`state`, `reason`) |
| interrupt requested with an attempt | `interrupt` (`phase: requested`, `interrupt_id`, `kind`, `expires_at`, `continuation_no`) after the attempt's `result` |
| interrupt answered | `interrupt` (`phase: resolved`) |
| interrupt rejected by an operator | `interrupt` (`phase: rejected`), then `terminal` (`state: operator_rejected`, `reason`) |
| interrupt expired | `interrupt` (`phase: expired`), then `terminal` (`state: expired`) |
| continuation begun / ended | `job-kit:continuation-started` / `job-kit:continuation-result`, then `terminal`, or `interrupt` for a follow-up request |

`interrupt` events use the envelope `plugins-kit.execution-event/v2`; every
other event stays `plugins-kit.execution-event/v1`, so a run that never
interrupts produces only `plugins-kit.execution-event/v1` events. No event carries a request
payload, a request schema or a resolution input; the operator's rejection
reason is the one free-text field, on `terminal`.

Identity: `run_id` is the run, `unit_id` the job id, and `attempt_id` the
attempt number as a string. `source.adapter` and `source.model` are the
attempt's backend and model. A job reaches exactly one `terminal`.

`seq` is unique across the whole run and follows the order the ledger recorded
the facts, including across `resume`. `at` is informational: it is the fact's
own timestamp when that is a UTC time, and the moment of recording otherwise.
A token count of 0 that means "not reported" is recorded as `null`, so a
total-only report stays total-only.

Run and job ids must fit the envelope (at most 200 characters, no control
characters); `run` refuses a job file that breaks this before writing
anything. A run created by a job-kit that predates the event log has no events
for its earlier facts, so `events` refuses to export it rather than present a
partial stream as its history.

Recording events needs bootstrap >= 0.136.0, the release that supports the
`/v2` envelope. With an older or absent `bootstrap_lib`, a write refuses before
anything is recorded and names the `claude plugin update` or
`claude plugin install` command that fixes it.

## Durable interrupts

A person can be a step in a run. A contract asks a question; the job waits,
durably, with no process alive; an operator answers with `job-kit resolve`;
`job-kit resume` continues. A wait is a healthy state, never a failure. The
request, answer and resolution rules run in `bootstrap_lib.interrupt_contract`
(bootstrap >= 0.137.0, probed before the ledger opens); its specification is the
plugin-dev skill's `references/interrupt-contract.md`.

### Job states and how status tells them apart

`job-kit status` distinguishes these outcomes by the recorded job state:

| State | Meaning |
| --- | --- |
| `waiting` | a valid interrupt request is open; not terminal |
| `operator_rejected` | an operator rejected the request; terminal |
| `expired` | the request lapsed before it was resolved; terminal |
| `failed` | the job failed; terminal |
| `halted` | provider-halted (auth, rate limit, credit or spent quota); terminal |

`rejected` (the contract refused the output) and `unroutable` remain distinct.
A run whose jobs are neither running nor pending, not all terminal, and at
least one `waiting`, has run status `waiting`. A waiting job holds no
reservation and spends no attempt budget, and its worktree is kept.

### The request envelope

A contract requests an interrupt by writing a JSON file to the path in
`JOB_KIT_INTERRUPT_REQUEST` (the file does not exist yet; stdout and stderr are
never read for state):

```json
{
  "schema": "job-kit.interrupt-request/v1",
  "kind": "approval",
  "request_schema": {"type": "object", "required": ["approved"],
                     "properties": {"approved": {"const": true}}},
  "payload": {"action": "push release tag", "target": "v1.2.0"},
  "expires_in_s": 86400
}
```

The file is UTF-8, at most 131072 bytes, and has exactly the keys `schema`,
`kind`, `request_schema`, `payload` and the optional `expires_in_s`. `kind`
matches `[a-z][a-z0-9-]*` (at most 64 characters). `request_schema` must use
only the JSON Schema subset that llm-scripting-kit's `completion.json_schema`
supports; a schema using any other keyword is refused. `payload` is a
JSON-native mapping. `expires_in_s` is an integer from 1 to 2147483647 seconds.
The exit code still governs the attempt:

| Contract exit | Request file | Attempt outcome |
| --- | --- | --- |
| 0 | absent | accepted |
| 0 | valid | the job waits (`waiting`, acceptance outcome `interrupt_requested`) |
| 0 | present, invalid | failed, acceptance outcome `request_refused`, `error_code` `interrupt_request`, naming the fault |
| non-zero | absent | rejected per the attempt budget |
| non-zero | present | failed, acceptance outcome `request_refused`, `error_code` `interrupt_request` |
| timed out or not run | any | decided by the timeout or not-run rule; the file is not read |

At most one request is open per job, and one request per contract run.

### Resolving

```bash
job-kit resolve <run-id> <interrupt-id> --input '{"approved": true}'
job-kit resolve <run-id> <interrupt-id> --input-file answer.json
job-kit resolve <run-id> <interrupt-id> --reject --reason "not this release"
```

Exactly one of `--input`, `--input-file` and `--reject` is required; `--reason`
is only valid with `--reject`. The interrupt id is in `job-kit status`
(`interrupts`). An answer is validated against the request schema recorded with
the interrupt, is at most 65536 bytes as canonical JSON, and must be JSON-native
(no NaN or infinity, no repeated keys). `--input-file` exists because quoting
JSON on a Windows command line is error-prone. `resolve` prints one JSON object:
`{run, interrupt_id, job_id, outcome, decision, state, store}` with `outcome`
`recorded` or `replayed`, or a refusal `{run, interrupt_id, refused, message,
errors}` where `refused` is `schema`, `input`, `invalid_json`, `conflict` or
`expired` and `errors` holds `{pointer, keyword}` entries for a schema refusal.

| Exit | Meaning |
| --- | --- |
| 0 | recorded, or replayed |
| 1 | refused: schema, oversized or non-JSON input, conflicting resolution, or expired |
| 3 | job-kit failed: no store, unknown run or interrupt, or llm-scripting-kit cannot validate |

The resolution is immutable. Replaying the same decision with the same input
(compared as canonical JSON) exits 0 and writes nothing, even after the job has
moved on. A different decision or input for an already-resolved interrupt is a
conflict and changes nothing. An answer leaves the job `waiting`; a rejection
ends it `operator_rejected`. `resolve` never resumes the run: run
`job-kit resume <run-id>`. It is safe beside a live `run` or `resume`; an
answer that arrives mid-pass is continued by the next `resume`.

llm-scripting-kit must be able to validate the schema before `run`, `resume`
and `resolve` touch the ledger; if it cannot, the verb exits 3 before opening,
migrating or writing anything.

### Expiry

No timer or daemon runs. An interrupt with an expiry lapses at its `expires_at`
(inclusive), and the lapse is recorded by the first writing verb that sees it:
`resume` records it before deciding what to dispatch, and `resolve` records it
and then refuses. `status` never records anything. For a waiting job whose
interrupt has lapsed, `status` shows the recorded `state` (`waiting`) beside an
`effective_state` of `expired`, and marks the interrupt `lapsed`; both are
shown, so a lapse nobody has recorded yet is visible.

### The continuation contract

After an interrupt is answered, `job-kit resume` re-runs the job's contract for
the attempt that raised it, with no model call, no new attempt and no attempt
budget spent:

- stdin is that attempt's recorded response text, and the working directory is
  its recorded workspace;
- the identity variables of that attempt are set, plus `JOB_KIT_INTERRUPT_ID`,
  `JOB_KIT_CONTINUATION_NO` (1, 2, ... per attempt) and
  `JOB_KIT_INTERRUPT_RESOLUTION`, the path of a job-kit-written file:

```json
{
  "schema": "job-kit.interrupt-resolution/v1",
  "interrupt_id": "7",
  "kind": "approval",
  "outcome": "answered",
  "input": {"approved": true},
  "payload": {"action": "push release tag", "target": "v1.2.0"},
  "resolved_at": "2026-09-30T10:00:00Z"
}
```

- a contract tells its first run from a continuation by the presence of
  `JOB_KIT_INTERRUPT_RESOLUTION`; a continuation may request a follow-up
  interrupt through a fresh `JOB_KIT_INTERRUPT_REQUEST`;
- exit 0 with no request accepts the job; exit 0 with a request waits again;
  a non-zero exit rejects it when the attempt was the last budgeted one and
  otherwise returns it to `pending` for a fresh attempt.

**A continuation runs at least once, not exactly once.** If the process is lost
or interrupted (Ctrl-C) during a continuation, the job returns to `waiting` with
its answer intact and the next `resume` runs the contract again. job-kit cannot
observe what a killed contract did before it died, so it cannot make a side
effect idempotent: a contract that performs a side effect after approval must
make that side effect idempotent itself. What job-kit guarantees is a stable
key: every run of the continuation for one interrupt receives the same
`JOB_KIT_INTERRUPT_ID` and a byte-identical resolution document (canonical JSON
built only from the recorded interrupt and resolution, with the recorded
`resolved_at`). Use those as the idempotency key. `JOB_KIT_CONTINUATION_NO`
changes on every re-run and is a counter, not a key.

### What reads and what migrates

`job-kit status` is read-only: it opens the ledger read-only, sets no journal
mode and never migrates it, so it can be pointed at a ledger written by an
older job-kit (schema 10 or later) without upgrading it. Below schema 12 it
reports no interrupts. Every other verb (`run`, `resume`, `resolve`, `events`,
`gc`) opens the ledger read-write, migrating it when needed, and a ledger opened
by a newer job-kit is refused by an older one. `status` reports
`effective_state` per job, `interrupts` (with request schema, payload and
resolution) and `continuations` per run, and `read_at`. The request payload
and schema appear in `status` because the operator needs them to answer; they
never appear in events.
