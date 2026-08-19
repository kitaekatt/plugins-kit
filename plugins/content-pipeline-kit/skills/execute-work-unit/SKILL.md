---
_schema_version: 1
name: execute-work-unit
author: christina
skill-type: technique-skill
description: Use when acting as a content-pipeline background worker completing exactly one work unit through a consumer's protocol mount. Do NOT use for orchestrating a whole run (see background-pipeline) or for any interactive, non-worker use of the pipeline.
---

# Execute Work Unit

The procedure for completing exactly one content-pipeline work unit as a
Claude background session launched by the dispatcher
(`execution/drivers/claude_bg.py::dispatch_wave`).

Every such worker is governed by its **launch prompt**, which the dispatcher
builds (`build_launch_prompt`) and which stands on its own: it names the run
id, unit id, worker id, and answer path, lists the exact invocations the
worker may run, and states the rule against composing a shell construct to
satisfy a step. That is true of every worker the dispatcher launches, whether
or not any agent definition is in play.

This skill is that same procedure at length. `agents/pipeline-worker.md` is
an agent definition a consumer may select at launch by passing the
agent-selecting `claude` flags through `dispatch_wave`'s `extra_launch_args`;
the dispatcher selects no agent on its own, and whether those flags compose
with a background launch rather than being dropped is not established. So
read this skill as the worker procedure, not as evidence that a particular
agent definition was loaded.

The procedure is stated as **enumerated invocations** -- exact command
strings, computed by the library before the worker ever runs -- never as an
outcome description a worker is free to satisfy however it likes. That
framing is load-bearing: a
2026-08-17 probe stalled forever on a shell redirect (`echo ... > file`) a
worker composed itself to satisfy an instruction phrased as a desired outcome;
no allowlist author would have enumerated that construct in advance, so
nothing authorized it and nothing could complete it. Every invocation below
must therefore be run **exactly as written**, never approximated,
paraphrased, or reconstructed via a different shell mechanism.

## Where the exact invocations come from

`content_pipeline.execution.drivers.claude_bg.enumerate_worker_invocations`
takes a `WorkerCommand` (the consumer's protocol-mount template) plus a run
id, unit id, and worker id, and returns exactly five deterministic strings:
`claim`, `read`, `submit --from-file <answer path>`, `fail`, and the Write-tool
target for the answer file. Deterministic in `(run_id, unit_id, worker_id)`
alone -- no unit content, no timestamp, no random component -- which is what
makes a pre-authorized allowlist entry for a worker session possible at all: a
mount owner can compute and allowlist the exact five strings for a unit before
that unit's worker session ever launches.

The block below shows those five invocations for one fully worked example
(`WorkerCommand(argv=("python", "mount.py", "run"), answer_dir="/path/to/answers")`,
run id `RUN_ID`, unit id `UNIT_ID`, worker id `WORKER_ID`) so the shape is
concrete. A worker's actual launch prompt carries the same five strings
computed for its own real run id, unit id, worker id, and the consuming
project's own `WorkerCommand` -- never this example's literal values.

### Generated invocation strings

The fenced block below, wrapped in a pair of HTML comment markers naming
"ENUMERATED-INVOCATIONS" (`BEGIN`/`END`, visible in the raw source of this
file just above the block), is generated from
`enumerate_worker_invocations`'s actual output for the example inputs above.
These strings are generated, not hand-composed -- do not edit them by hand.

`answer_path_for` (and so `enumerate_worker_invocations`'s `submit`/`write`
strings) builds its path with a native path separator -- backslash on
Windows, forward slash on macOS/Linux. The block below is written with
forward slashes, the more readable spelling for a consumer doc and the one a
Windows path also satisfies once compared separator-insensitively.

<!-- BEGIN ENUMERATED-INVOCATIONS -->
```text
claim: python mount.py run claim --run-id RUN_ID --unit-id UNIT_ID --worker-id WORKER_ID
read: python mount.py run read --run-id RUN_ID --unit-id UNIT_ID --worker-id WORKER_ID
write: Write tool -> /path/to/answers/RUN_ID__UNIT_ID.answer.txt
submit: python mount.py run submit --run-id RUN_ID --unit-id UNIT_ID --worker-id WORKER_ID --from-file /path/to/answers/RUN_ID__UNIT_ID.answer.txt
fail: python mount.py run fail --run-id RUN_ID --unit-id UNIT_ID --worker-id WORKER_ID
```
<!-- END ENUMERATED-INVOCATIONS -->

## Procedure

Perform exactly these steps, in this order, and no others. Do not compose a
redirect, pipe, or any other shell construct to satisfy any step below -- run
the invocation exactly as your own launch prompt states it (the prompt built
by `build_launch_prompt`, carrying your run's real values in place of this
skill's `RUN_ID`/`UNIT_ID`/`WORKER_ID` example).

1. **Claim the unit** -- run the `claim` invocation.
2. **Read the prepared request** -- run the `read` invocation. This is the
   only step that returns unit content; nothing before it may be reasoned
   about as if it were unit content.
3. **Write your answer** -- produce your answer text and write it, verbatim,
   with the Write tool, to exactly the answer path named in your launch
   prompt (no other path).
4. **Submit your answer** -- run the `submit` invocation. If the submission
   is rejected with feedback, revise the answer file (step 3) and repeat this
   step. There is no invocation for "give up cleanly" other than step 5.
5. **On exhaustion, report failure** -- if you cannot produce an answer the
   validators accept, run the `fail` invocation and stop. Never fabricate an
   answer to close the unit out instead.

See `agents/pipeline-worker.md` for the full behavioral discipline around this
procedure (unit-content handling, the revision loop, and why exhaustion means
exit rather than fabrication).
