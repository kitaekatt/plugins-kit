# job-kit

Durable sequential execution for heterogeneous agent jobs.

A job file declares work; the runner executes each job once, selects an
endpoint from what llm-scripting-kit actually advertises, and accepts a result
only when a command says so.

```bash
job-kit run jobs.yaml [--store PATH] [--timeout SECONDS] [--run-id ID]
job-kit status <run-id> [--store PATH]
job-kit resume <run-id> [--store PATH] [--timeout SECONDS]
job-kit gc [<run-id>] [--store PATH] [--accepted-only] [--force]
```

`--run-id` preassigns the run's identity, so a caller that backgrounds a run
already knows what to pass to `status` and `resume`. Without it the runner
generates one and prints it with the final snapshot -- which is too late to
poll. `--timeout` is job-kit's own per-attempt budget and defaults to 900
seconds.

Exit codes: **0** every job accepted (or the verb succeeded), **1** a job was
rejected, failed, halted, or could not be routed, **2** a usage error, **3**
the runner itself failed. An unattended caller should branch on 1 versus 3:
the first is a result about the work, the second is a result about job-kit.

## The job file

```yaml
jobs:
  - id: lint
    prompt:
      system: "You are a coding assistant."
      user: "Fix the lint errors."
    endpoint_preference: [qwen38-5090, luna, sonnet]
    requirements:
      params: [cwd]
    directory: .
    contract:
      command: [python, -m, pytest, tests/lint]
```

## What it gives you

- **Advertisement-based endpoint selection.** `endpoint_preference` is tried in
  order, but an endpoint is skipped unless it advertises the `requirements` the
  job states. Preference alone would dispatch a job to a backend that cannot
  serve it; the requirements are what make the order safe. The `requirements`
  mapping is llm-scripting-kit's requirement language over an adapter's
  advertised `Capabilities` -- see llm-scripting-kit's README, "Capability
  requirements" subsection, for the named convenience keys and dotted-path
  fallback. job-kit consumes that language and that advertisement; it does not
  define its own. Requires llm-scripting-kit >= 0.23.0, the version that added
  `match_capabilities`; job_kit.select fails at import time with a named
  remediation if an older llm-scripting-kit is linked in.
- **Command-shaped acceptance.** A `contract` command must exit zero for the
  attempt to be accepted. Model output that does not satisfy it is a failure,
  not a result, so nothing downstream has to trust the text.
- **Durability.** Runs are recorded. `status` reports one, `resume` continues
  the non-terminal jobs of one, and an interrupted run does not restart from
  the beginning. Every attempt is a row: job-kit never retries inside an
  attempt, so the ledger's attempt count is the invocation count.
- **Halts narrow the run; timeouts do not.** An endpoint that returns a
  persistent halt is excluded from the rest of the run. A `--timeout` expiry is
  job-kit's own budget rather than evidence about the endpoint, so it is
  recorded as a retryable timeout and the endpoint stays eligible.
- **Worktree-per-attempt isolation** for jobs in a git repository, so a failed
  attempt leaves no residue. Set `workspace.isolate: false` for a job that must
  run in its declared directory.
- **A tool deny floor.** The job-file-level `disallowed_tools` applies to every
  job in the run. Harness endpoints are agent sessions, not plain completions;
  the floor is how a run declares what they may not do.

## Options

A job's optional `options` mapping carries `allowed_tools`,
`disallowed_tools`, `effort`, `system_prompt_mode`, `max_tokens`, `temperature`
and `extras` through to the completion seam. `max_tokens` must be at least 1
and `temperature` must be in the range 0 to 2. If omitted, they default to 4096
and 0.3. `effort` overrides the reasoning effort the endpoint registry entry
carries: effort is a property of the ENDPOINT, so a job that needs more
deliberation than its endpoint's default says so here, and leaving it unset
emits exactly the argv an existing job file always did.

`workspace` accepts `directory`, `base_ref` and `isolate`.

Every path-typed field -- `directory`, `contract.directory`,
`workspace.directory`, `workspace_root` -- resolves relative to the job file's
own directory, so `directory: .` means the job file's directory and job files
need no absolute paths. Paths inside PROMPT text are just text and are not
resolved.

### What a contract receives

The contract command runs in the attempt's workspace with the completion text
on **stdin** and this attempt's identity in the environment: `JOB_KIT_RUN_ID`,
`JOB_KIT_JOB_ID`, `JOB_KIT_ATTEMPT_NO`, `JOB_KIT_ENDPOINT`, `JOB_KIT_BACKEND`,
`JOB_KIT_MODEL`.

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
  before `run`), because a worktree is discarded;
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
