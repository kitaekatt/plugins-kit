# Declarative workflows: the `.workflow.yaml` format and flow

A human authors a workflow once as `*.workflow.yaml`; workflow-kit validates it,
compiles it to a native Workflow tool script, and runs it. The compile step is
deterministic Python (no model); only the final run hands the compiled script to
the native Workflow tool. *workflows is for Claude to make workflows;
workflow-kit is for humans to make workflows with Claude.*

**Invoking the skill to run a workflow IS the user's opt-in to the native
Workflow tool.**

Workflows live at `<project>/.claude/workflows/<name>.workflow.yaml`. Compiled
scripts are derived artifacts at
`<project>/.claude/workflows/.compiled/<name>.js` (gitignored). The plugin ships
an example under `${CLAUDE_PLUGIN_ROOT}/examples/`.

The compiler runs through the plugin-venv interpreter (NOT `uv run python`, which
resolves the venv from cwd and breaks from a foreign project root):

- Windows: `~/.claude/plugins/data/plugins-kit/workflow-kit/.venv/Scripts/python.exe`
- macOS/Linux: `~/.claude/plugins/data/plugins-kit/workflow-kit/.venv/bin/python`

## Procedure: compile and run

PRECONDITION: confirm `~/.claude/plugins/data/plugins-kit/workflow-kit/bootstrap.log`
exists before invoking the plugin-venv interpreter; if missing, tell the user
"the bootstrap plugin hasn't provisioned workflow-kit -- install/enable
plugins-kit:bootstrap and start a new session" and STOP.

1. **Resolve the workflow file.** If the user named a path, use it. Otherwise
   look in `<project>/.claude/workflows/<name>.workflow.yaml`, then
   `${CLAUDE_PLUGIN_ROOT}/examples/`. If none, list available `*.workflow.yaml`
   files and ask which.
2. **Compile it.** Run the compiler via the plugin-venv interpreter, writing the
   script under `.compiled/`:
   `<plugin-venv-python> ${CLAUDE_PLUGIN_ROOT}/scripts/compile_workflow.py <yaml> -o <project>/.claude/workflows/.compiled/<name>.js`
   Exit 0 -> stdout is the compiled path. On exit 1, surface stderr (an authoring
   error) and STOP -- do not run.
3. **Gather inputs.** Read the `inputs:` block; for each declared input collect a
   value from the user's request (ask via AskUserQuestion only if a required
   input is missing and cannot be inferred). Assemble into an args object. **If the
   workflow uses any `script` or `openrouter` node, also inject the reserved args**
   the compiled script references (not declared in `inputs:`):
   - `runId` -- a short id for this run (namespaces `$OUT` paths).
   - `pluginRoot` -- `${CLAUDE_PLUGIN_ROOT}` for workflow-kit.
   - `workflowKitVenvPython` -- the plugin-venv python
     (`~/.claude/plugins/data/plugins-kit/workflow-kit/.venv/{Scripts/python.exe|bin/python}`);
     when workflow-kit is dev-only (no own venv), use the bootstrap standalone python.
4. **Run it.** Pass the compiled path as `scriptPath` and the args object as `args`
   to the native Workflow tool. This is the opt-in boundary. (The compiled script
   normalizes `args` itself -- this runtime delivers it as a JSON string.)
5. **Relay the result** to the user in readable form (the tool result is not
   shown to them directly).

Checklist: compiler exited 0 before any Workflow-tool call; `args` keys match the
declared inputs; results summarized, not raw.

Gotchas:
- Never run the compiled `.js` by hand or with `node` -- only the native Workflow
  tool executes it.
- Do not edit files under `.compiled/` -- they regenerate from the `.workflow.yaml`.
- If the compiler errors, fix the `.workflow.yaml`, not the generated JS.

## Procedure: validate only

Run the compiler in validate-only mode (same precondition as above):
`<plugin-venv-python> ${CLAUDE_PLUGIN_ROOT}/scripts/compile_workflow.py <yaml> --validate-only`
Exit 0 prints `OK: <name> (<n> step(s))`. Exit 1 prints a located error to
stderr -- relay it verbatim.

## Procedure: scaffold

Ask for the workflow name and a one-line description. Write a starter file to
`<project>/.claude/workflows/<name>.workflow.yaml` using the format below (a
`name`, `description`, an `inputs:` block, one `schemas:` entry, and a `steps:`
list with one example agent step), using
`${CLAUDE_PLUGIN_ROOT}/examples/review-changes.workflow.yaml` as the reference.
Then offer to validate it.

## The format (v1)

See `${CLAUDE_PLUGIN_ROOT}/examples/review-changes.workflow.yaml` for a complete
example. Top-level keys: `name`, `description` (required); `inputs`, `phases`,
`schemas`, `output` (optional); `steps` (required, ordered).

A **step** is exactly one of: an agent step, a pipeline step, a `script` node, or an
`openrouter` node.

- **agent step** -- `agent: { prompt, schema?, model?, agentType?, isolation?, label? }`.
  Add `for_each: "{{ ... }}"` + `mode: parallel` to fan out (the item is bound to
  `item`).
- **pipeline step** -- `pipeline: { over, as, stages: [...] }`. Each stage is an
  agent step; a stage may add `fan_out: { over, as, mode }` to fan out within the
  stage. Stages run with no barrier (item A reaches stage 2 while item B is still
  in stage 1).
- **script node** -- `script: { command, out?, status?, label? }`. Runs `command`
  (a shell command, templated) via the workflow-kit-agent executor with stdout
  captured to `$OUT`. `out` defaults to `./.workflow-kit/{{runId}}/<step-id>.out`.
  Add `for_each` to fan out (the index `i` is appended to the default out path so
  payloads do not collide). See `node-strategies.md`.
- **openrouter node** -- `openrouter: { prompt_file, model?, cheap?, system?, out?,
  status?, label? }`. One non-Claude model call whose reply lands in `$OUT`.
  `prompt_file` is a path (an input, or an upstream node's `{{ steps.ID.path }}`).
  Omit `model` to use llm-scripting-kit's configured `default` (or set `cheap: true`
  for `defaultCheap`); or pass a registry alias / raw slug. See `node-strategies.md`.

**Templating** -- `{{ inputs.X }}`, `{{ steps.ID }}`, `{{ steps.ID[*].field }}`
(flatten), `{{ <as> }}` (pipeline/fan_out item), `{{ <prevStage>.field }}`
(preceding stage's result). Anything outside this grammar is a compile error.

### Reserved inputs (auto-injected for node steps)

`script` and `openrouter` nodes need three runtime values the skill supplies
automatically (do NOT declare them in `inputs:`; the run procedure injects them):

- `{{ inputs.runId }}` -- a per-run id used to namespace default `$OUT` paths.
- `{{ inputs.pluginRoot }}` -- the workflow-kit plugin dir (for the openrouter runner).
- `{{ inputs.workflowKitVenvPython }}` -- the interpreter that runs the openrouter
  runner and your `script` Python commands. Reference it in a `script` `command`
  instead of bare `python` (which the Windows Store stub hijacks).

A node step's result is its executor metadata `{ exit_code, path, bytes, sha256,
status }`; a downstream `agent` step reads the payload via `{{ steps.ID.path }}` --
that is the only place the file body enters a context.
