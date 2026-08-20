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
id, unit id, worker id, answer path, and fencing token, lists the exact
invocations the worker may run, and states the rule against composing a shell
construct to satisfy a step. That is true of every worker the dispatcher
launches, whether or not any agent definition is in play.

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
id, unit id, and worker id, and returns exactly six deterministic strings:
`read`, `submit --text-file=<answer path>`, `fail` (each of these three is
`<argv> protocol @<envelope path>` -- one JSON envelope file per verb, read
via the library's `@<path>` envelope-sourcing form), plus three Write-tool
targets: the answer file, the `submit` envelope, and the `fail` envelope.
Deterministic in `(run_id, unit_id, worker_id)` alone -- no unit content, no
timestamp, no random component, and critically no fencing token -- which is
what makes a pre-authorized allowlist entry for a worker session possible at
all: a mount owner can compute and allowlist the exact six strings for a unit
before that unit's worker session ever launches.

There is no `claim` invocation. The dispatcher claims the unit itself before
the session launches, and the fencing token that claim produced is named in
the launch prompt. A worker therefore starts at `read`, and has no way to
take a claim at all -- which is what keeps a session left over from an
earlier dispatch of the same unit from taking the claim back.

The `read` envelope file is pre-written by the dispatcher (before the worker
session ever launches, since its content needs no runtime information); the
`submit`/`fail` envelope files are authored by the WORKER itself, at runtime,
by writing the library's own template text and substituting only the literal
`<FENCING_TOKEN>` placeholder with the fencing token its launch prompt names
-- see step 3 of the procedure below.

The block below shows those six invocations for one fully worked example
(`WorkerCommand(argv=("python", "mount.py", "run"), answer_dir="/path/to/answers", envelope_dir="/path/to/envelopes")`,
run id `RUN_ID`, unit id `UNIT_ID`, worker id `WORKER_ID`) so the shape is
concrete. A worker's actual launch prompt carries the same six strings
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
read: python mount.py run protocol @/path/to/envelopes/RUN_ID__UNIT_ID.read.json
submit: python mount.py run protocol @/path/to/envelopes/RUN_ID__UNIT_ID.submit.json --text-file=/path/to/answers/RUN_ID__UNIT_ID.answer.txt
fail: python mount.py run protocol @/path/to/envelopes/RUN_ID__UNIT_ID.fail.json
write: Write tool -> /path/to/answers/RUN_ID__UNIT_ID.answer.txt
write: Write tool -> /path/to/envelopes/RUN_ID__UNIT_ID.submit.json
write: Write tool -> /path/to/envelopes/RUN_ID__UNIT_ID.fail.json
```
<!-- END ENUMERATED-INVOCATIONS -->

## Procedure

Perform exactly these steps, in this order, and no others. Do not compose a
redirect, pipe, or any other shell construct to satisfy any step below -- run
the invocation exactly as your own launch prompt states it (the prompt built
by `build_launch_prompt`, carrying your run's real values in place of this
skill's `RUN_ID`/`UNIT_ID`/`WORKER_ID` example). Your unit is already
reserved for you and your launch prompt names your **fencing token**; that
prompt is the only place the token comes from, and no invocation below
returns one. The dispatcher has already written your `read` envelope file
before your session launched; you have two of your own envelope files to
author yourself, at the points below.

1. **Read the prepared request** -- run the `read` invocation. This is the
   only step that returns unit content; nothing before it may be reasoned
   about as if it were unit content.
2. **Write your answer** -- produce your answer text and write it, with the
   Write tool, to exactly the answer path named in your launch prompt (no
   other path). The **first line of that file must be the fence line your
   launch prompt gives you** -- `content-pipeline-fence:` followed by your
   fencing token -- and your answer text follows on the next line, verbatim
   and unaltered. That first line is what proves the text was produced under
   your claim rather than by an earlier worker for the same unit; a file
   without it is refused, and so is one declaring a different token.
3. **Author your submission envelope** -- with the Write tool, write the
   `submit` envelope template your launch prompt gives you, substituting
   ONLY the literal token `<FENCING_TOKEN>` with the fencing token your
   launch prompt names -- to exactly the `submit` envelope path named in
   your launch prompt (no other path, nothing else in the template changed).
4. **Submit your answer** -- run the `submit` invocation. If the submission
   is rejected with feedback, revise the answer file (step 2, fence line
   included) and repeat this step -- the submission envelope from step 3
   does not change and must not be rewritten. There is no invocation for
   "give up cleanly" other than step 5.
5. **On exhaustion, report failure** -- if you cannot produce an answer the
   validators accept, author your failure envelope the same way as step 3
   (substituting only `<FENCING_TOKEN>`), then run the `fail` invocation, and
   stop. Never fabricate an answer to close the unit out instead.

See `agents/pipeline-worker.md` for the full behavioral discipline around this
procedure (unit-content handling, the revision loop, and why exhaustion means
exit rather than fabrication).
