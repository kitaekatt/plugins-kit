# OpenCode dispatch mechanics

The exact flags, command shape, and traps for launching a unit on the OpenCode
CLI backend. When you have already CHOSEN OpenCode, load this before you compose
the launch. The rendered policy carries the summary. This reference carries the
detail.

Rendered policy: `scripts/orchestration_guidance.py`. Backend record and the
one-line command: `defaults/orchestration.yaml`, `backends[id: opencode]`.
Command source: `llm_scripting_kit.harness_adapters.OpencodeAdapter`. The
adapter builds argv only. The launch adds the stream redirects shown below.

## When a harness is warranted at all

A harness supplies an agent loop, tools, instruction-file ingestion, and a
working directory. If the unit must discover inputs, inspect files, run checks,
iterate, or edit in place, use the harness. A pure transformation of context
already in the brief belongs on a completion endpoint instead.

## Absolute paths, always

Pass an absolute `--dir`, and use absolute paths for the brief, result, and log
files. The adapter rejects a relative working directory. More importantly, a
background launch cannot rely on a preceding `cd` persisting.

`--dir` selects the working directory only. It is not a writable-root boundary
and does not restrict paths outside that directory.

## The one invocation

The backend record's one-line command is:

  opencode run --pure --dir <ABSOLUTE root> -m <MODEL> --agent build --auto

Complete it at launch with stdin and output capture:

  opencode run --pure --dir <ABSOLUTE root> \
    -m <MODEL> --agent build --auto \
    < <ABSOLUTE brief file> \
    > <ABSOLUTE result.md> 2> <ABSOLUTE log.txt>

The rendered policy's command uses one discovered entry as an example. Before
every launch, take `<entry-id>` from the chosen `opencode/<entry-id>` target
and resolve it:

  llm-scripting-kit resolve --endpoint <entry-id> \
    --project-root <ABSOLUTE root>

Read the returned JSON. Replace the displayed `-m` value with its `model`.
Remove any displayed `--variant`, then add `--variant <effort>` only when the
returned `effort` is non-null. Never reuse the example model when routing chose
another entry.

The flags and streams have these jobs:

  `run`             Starts the non-interactive one-shot command.
  `--pure`          Runs without external plugins.
  `--dir <DIR>`     Sets the working directory. It confines nothing.
  `-m <MODEL>`      Selects the fully qualified provider/model value from the
                    resolved registry entry.
  `--agent build`   Selects OpenCode's file-and-tool-capable build agent.
  `--auto`          Makes a background run non-interactive. It bypasses
                    permission prompts by auto-approving anything not
                    explicitly denied. It adds no sandbox or write scope.
  stdin             Carries the brief from a file. Never put a real brief in a
                    shell argument.
  stdout            Carries the answer. OpenCode has no `-o` result-file flag,
                    so redirect stdout to `result.md`.
  stderr            Carries diagnostics. Preserve it in `log.txt`.

Provider-specific effort is an optional per-unit addition:

  `--variant <VALUE>`  Selects a provider-defined variant such as a reasoning
                       effort. OpenCode has no backend-wide variant menu. Use
                       only a value the selected provider defines. Without one,
                       effort is not dialable on this backend.

Network is available to the local process without an enablement flag.

Verify that the brief's reads, writes, commands, and external effects are
already within the user's authorization. If not, obtain approval before
dispatch. `--auto` removes the child harness's approval prompt for anything
not explicitly denied. User-configured denies still apply.

Launch through the harness's background mechanism. Do not add `nohup`, a
trailing `&`, or any other shell backgrounding. Then end the turn. The harness
re-invokes the orchestrator after the process exits.

## Exit 0 is not success

The process exit only says the invocation returned. Treat a missing or empty
`result.md` as a failed return. Read the result. Verify claimed file work
against the actual diff. `log.txt` is diagnostic evidence, not the answer.

## What the sandbox does and does not bound

There is no sandbox. Under `--auto`, the adapter supplies no read, write, or
network boundary. Explicit user-configured denies can still refuse tools.
`--dir` supplies context. It does not create a boundary.

Control workspace edits at the join. Require a clean tree at dispatch. Inspect
the diff on return. Give each concurrent writer its own worktree. Brief only
effects already authorized for the unit.

## Monitoring

If live progress matters, tail `log.txt` once after launch. Use the harness's
monitoring facility after that. Do not poll the result path or sleep.

## Parallel isolation

Give every parallel writer a separate git worktree and pass its absolute path
to `--dir`. Read-only units can share a tree.

OpenCode itself documents no concurrency limit. A locally hosted model entry
can point to a hand-started server that SERIALIZES concurrent requests. Obey
the routing row's limit for that entry. A dead server is a transport error that
falls through to the next model. That fallback is intended behaviour.

## Collecting

When the background launch completes, read `result.md` once. Keep `log.txt` out
of the orchestrating context unless a specific claim needs diagnosis. Judge
write-work by both the result and the worktree diff.

## Custom providers: model entries own the specifics

The model registry owns the exact provider/model string and any default
variant. The adapter passes that model through `-m`. It passes a non-empty
variant through `--variant`. The public backend record does not duplicate
provider, server, or machine details.
