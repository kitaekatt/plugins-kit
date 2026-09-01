# job-kit

Durable sequential execution for heterogeneous agent jobs.

A job file declares work; the runner executes each job once, selects an
endpoint from what llm-scripting-kit actually advertises, and accepts a result
only when a command says so.

```bash
job-kit run jobs.yaml
job-kit status <run-id>
job-kit resume <run-id>
job-kit gc
```

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
  define its own.
- **Command-shaped acceptance.** A `contract` command must exit zero for the
  attempt to be accepted. Model output that does not satisfy it is a failure,
  not a result, so nothing downstream has to trust the text.
- **Durability.** Runs are recorded. `status` reports one, `resume` continues
  the non-terminal jobs of one, and an interrupted run does not restart from
  the beginning.
- **Worktree-per-attempt isolation** for jobs in a git repository, so a failed
  attempt leaves no residue. Set `workspace.isolate: false` for a job that must
  run in its declared directory.
- **A tool deny floor.** The job-file-level `disallowed_tools` applies to every
  job in the run. Harness endpoints are agent sessions, not plain completions;
  the floor is how a run declares what they may not do.

## Options

A job's optional `options` mapping carries `allowed_tools`,
`disallowed_tools`, `system_prompt_mode`, `max_tokens`, `temperature` and
`extras` through to the completion seam. `max_tokens` must be at least 1 and
`temperature` must be in the range 0 to 2. If omitted, they default to 4096 and
0.3. A contract receives the completion text on stdin and attempt metadata in
the `JOB_KIT_*` environment variables.
`workspace` accepts `directory`, `base_ref` and `isolate`.

Usage counts are nullable: a transport can complete without reporting tokens,
and unknown usage is recorded as unknown rather than as zero.
