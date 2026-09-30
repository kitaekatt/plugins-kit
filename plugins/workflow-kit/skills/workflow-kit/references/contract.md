# The workflow-kit node contract

A *node strategy* lets a native Workflow script run a deterministic shell command
(a script) or a non-Claude model call (openrouter) as a cheap, context-light
node. The native Workflow tool can't do this directly: its orchestrator body is
sandboxed (no filesystem, no shell, no network) and `agent()` spawns a Claude
subagent only. workflow-kit closes that gap with one convention -- the **node
contract** -- executed by the `workflow-kit-agent` executor on a haiku model.

This is the closest you get to a deterministic node while staying native. It is
NOT a deterministic runtime: a haiku agent still runs the command. The contract
is convention, lightly checked; correctness of the command is the author's job.

## The contract

A node is a shell command plus two paths:

- `$OUT` -- the file the command writes its **primary output** to. The payload
  lives here, on disk -- never in any model context.
- `$STATUS` -- (optional) a small JSON file the command may write for routing
  (e.g. `{"ok": true, "verdict": "PASS"}`). Small enough to travel in-context.

The command must:

1. Write its result to `$OUT` (the `script` strategy redirects stdout with
   `> $OUT`; the `openrouter` helper writes `$OUT` itself).
2. Exit `0` on success, non-zero on failure.
3. Be non-interactive (no prompts).

The `workflow-kit-agent` executor runs the command, never reads `$OUT`, and
returns only metadata:

```yaml
WORKFLOW_KIT_NODE_SCHEMA:
  exit_code: integer        # the command's exit status
  path: string              # = $OUT
  bytes: integer            # size of $OUT (0 if missing)
  sha256: string            # best-effort hash of $OUT (empty if unavailable)
  status: object | null     # $STATUS parsed as JSON, or null
  # required: exit_code, path, bytes
```

## Why payloads travel by file

Routing a command's output back through the model would cost tokens proportional
to the output size -- the classic route-data-through-context anti-pattern. By
writing to `$OUT` and returning only the path + metadata:

- **The orchestrator stays lean.** It holds path strings, not blobs. Route on
  `exit_code` / `status` (small), never on the file body (the sandboxed
  orchestrator can't read files anyway).
- **Deterministic / external hops cost ~no tokens.** A `script` node that
  produces a 5 MB file is as cheap as one that produces 5 bytes.
- **You pay the data cost once, at the point of genuine need.** When a real
  Claude reasoning node must reason over the payload, it `Read`s `$OUT` then --
  and only then do the tokens get spent.

So: control travels in-context (small, for routing); data travels on disk (large,
out of context).

## Proof of a payload: the verdict file

`exit_code` and `$STATUS` are metadata about the command, not proof that the
payload in `$OUT` is valid. A node that PROVIDES a named artifact (see
"Typed artifacts" in `node-strategies.md`) also writes a verdict file,
`workflow-kit.artifact-verdict/v1`, conventionally at
`./.workflow-kit/<runId>/<step>[.<i>].contract.json`. workflow-kit's own
code writes it after checking `$OUT` against the artifact's declared type:
a JSON Schema (validated with llm-scripting-kit's output-contract subset) or
an opaque file. It records `satisfied`, `violated` or `missing`, the size and
`sha256` of exactly the bytes judged, and the failing JSON pointers (never
payload values). A provider node exits non-zero unless the verdict is
`satisfied`, so routing on `exit_code` stops a consumer from reading an
invalid artifact. `$STATUS` is unchanged: it stays the command's own.

## Paths, fan-out, and resume

- **Deterministic, run-scoped paths.** Nondeterministic-time/random calls are
  banned in Workflow scripts (they break resume). Derive `$OUT` from a run id
  (passed via `args`) plus a node tag / index: e.g.
  `./.workflow-kit/<runId>/<tag>.out`. Distinct paths per parallel item so
  fan-out lanes don't collide. The executor does `mkdir -p` on the parent.
- **Resume caches metadata, not files.** `resumeFromRunId` returns a cached node
  result (`{path, sha256, ...}`) but does NOT restore the file. Two defenses:
  write artifacts under a run dir that persists with the run, and carry `sha256`
  so a downstream consumer can detect a stale/missing file and fail loudly
  rather than read garbage. For a provided artifact, the verdict file's
  `sha256` is the value to compare against.

## What this is not

- Not deterministic: an LLM (haiku) is in the loop. Keep the command's data on
  the shell/disk path (redirection) so the model never touches the payload --
  that minimizes deviation, but does not eliminate it.
- Not validated, for a node that provides no artifact: the executor checks
  exit code + file presence only, with no schema enforcement on the payload.
  A node that declares a provided artifact is checked by workflow-kit and
  writes a verdict file (above). No node is sandboxed. If a step needs bit-exact
  guarantees or moves large data with no downstream LLM consumer, do it in the
  main loop (and pass results in via `args`) instead of as a node.
