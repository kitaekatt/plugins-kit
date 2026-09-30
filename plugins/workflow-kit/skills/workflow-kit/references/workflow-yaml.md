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
   normalizes `args` itself -- this runtime delivers it as a JSON string.) If the
   tool rejects the `scriptPath` (it can reject a path it did not return itself), Read
   the compiled script and pass its full text as `script` instead; do not retry a
   respelled path.
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
  `item`). `model` is a model declaration: one registry id or a list of them
  (the format is bootstrap's plugin-dev `references/model-declaration.md`). An
  agent step runs on the harness, so it compiles to the FIRST Claude core id in
  the list (`fable`, `opus`, `sonnet`, `haiku`) and skips every other id without
  comment. A list with no core id is a compile error that names each declared id.
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
  `model` is a model declaration of llm-scripting-kit transport entry ids
  (e.g. `or-qwen`, or `[or-qwen, or-gpt-mini]`). Omit it to use the configured
  default declaration (set `cheap: true` for that entry's `defaultCheap`). A
  model alias or raw slug is not an entry id and is not accepted. Every
  compiled openrouter node also records its execution events at
  `./.workflow-kit/{{runId}}/<step-id>.events.jsonl` with unit id `<step-id>`;
  under `for_each`, the path gets the index `i` like `$OUT`, and the unit id is
  `<step-id>-<i>`. A re-run replaces the file. See `node-strategies.md`
  ("Execution events").

Any step may also declare `requires`, and a `script` or `openrouter` node may
declare `provides` -- see "Typed artifacts" below.

**Templating** -- `{{ inputs.X }}`, `{{ steps.ID }}`, `{{ steps.ID[*].field }}`
(flatten), `{{ <as> }}` (pipeline/fan_out item), `{{ <prevStage>.field }}`
(preceding stage's result), and, in a step that declares `requires`,
`{{ artifacts.NAME }}` (see "Typed artifacts"). Anything outside this grammar
is a compile error.

### Typed artifacts (`provides` / `requires`)

A node's `$OUT` can be a named, typed artifact that later steps require. The
compiler checks the whole contract before anything runs, and the node checks
the file after its command runs. See
`${CLAUDE_PLUGIN_ROOT}/examples/typed-contracts.workflow.yaml`.

```yaml
schemas:
  stats: { type: object, required: [lines], additionalProperties: false,
           properties: { lines: { type: integer } } }
steps:
  - id: count
    script: { command: '"{{ inputs.workflowKitVenvPython }}" wc.py "{{ inputs.source }}"' }
    provides:
      doc_stats: { schema: stats }        # a named schema from `schemas:`
  - id: classify
    openrouter: { prompt_file: "{{ inputs.source }}", cheap: true }
    provides:
      doc_class: { type: opaque-file }    # any regular file
  - id: reconcile
    requires:
      doc_stats: { schema: stats }
      doc_class: { type: opaque-file }
    agent: { prompt: "Read {{ artifacts.doc_stats }} and {{ artifacts.doc_class }}." }
```

- **Spec.** An artifact name is an identifier. A spec is exactly one of
  `schema: <name>` (a key of `schemas:`) or `type: opaque-file`. A `requires`
  spec may add `each: true` (default `false`).
- **`provides`** is allowed only on `script` and `openrouter` steps, with at
  most one entry: the artifact IS the node's single `$OUT`. An agent step's
  result is typed by its own `schema:` instead.
- **`requires`** is allowed on every step kind; on a pipeline step it covers
  every stage, `over` and `fan_out`.
- **Compile-time checks** (also run by `--validate-only`): every schema name
  exists; a provider schema is inside llm-scripting-kit's `validated-result`
  subset (no `pattern`, `format` or `oneOf`; the root refuses `null`) and its
  canonical JSON is at most 8192 bytes, because it travels on the node's
  command line (split it, or use `type: opaque-file` and validate
  downstream); an artifact has exactly one provider; a required artifact has
  a provider; the provider comes EARLIER in the document than the consumer
  (steps run in document order, and a step cannot require its own artifact).
- **Compatibility.** An `opaque-file` requirement accepts any provider. A
  `schema` requirement accepts only a `schema` provider whose schema digest
  (`OutputContract.schema_digest`, the sha256 of its canonical JSON) equals
  the requirement's: naming the same schema always matches, and two names
  with identical bodies match too, whatever their key order. A narrower or
  wider schema does not match. Cardinality must match: `each: true` exactly
  when the provider fans out with `for_each`.
- **`{{ artifacts.NAME }}`** compiles to the provider's executor-reported
  `$OUT` path (`steps.P.path`), or to the list of item paths for a fan-out
  provider (usable as a `for_each` or `over` value). It is valid only in a
  step whose `requires` names `NAME`, has no member tail, and is not
  available in `output:`. That use check runs at compile, not under
  `--validate-only`. A `requires` entry without an `artifacts.` use is still
  checked for order and type.
- **`artifacts` as a name.** In a document that declares `provides` or
  `requires`, `artifacts` cannot be a stage id or an `as` name (it is the
  expression head there). Step ids are unaffected. Documents without
  contracts are unchanged.
- **At run time** a script provider runs its command, then always runs
  `scripts/check_artifact.py` on `$OUT`; an openrouter provider validates
  the answer at the llm-scripting-kit seam. Each writes the verdict file
  `./.workflow-kit/{{runId}}/<step-id>[.<i>].contract.json` (format and
  exit codes: `node-strategies.md`, "Typed artifacts").
- **The guard.** After every provider step the compiled script checks the
  result: if the node, or any fan-out item, exited non-zero (a failed
  command, a `violated` or `missing` artifact, a refused schema), it throws
  an error naming the step, the artifact, the exit code and the verdict path,
  and no later step runs. Steps without `provides` get no check.

### Reserved inputs (auto-injected for node steps)

`script` and `openrouter` nodes need three runtime values the skill supplies
automatically (do NOT declare them in `inputs:`; the run procedure injects them):

- `{{ inputs.runId }}` -- a per-run id used to namespace default `$OUT` paths.
  It must be unique among runs in flight in one project directory: verdict
  and events files are per (runId, step, item) and have one writer each, so
  two concurrent runs sharing a `runId` can remove or overwrite each other's
  files.
- `{{ inputs.pluginRoot }}` -- the workflow-kit plugin dir (for the openrouter runner).
- `{{ inputs.workflowKitVenvPython }}` -- the interpreter that runs the openrouter
  runner and your `script` Python commands. Reference it in a `script` `command`
  instead of bare `python` (which the Windows Store stub hijacks).

A node step's result is its executor metadata `{ exit_code, path, bytes, sha256,
status }`; a downstream `agent` step reads the payload via `{{ steps.ID.path }}` --
that is the only place the file body enters a context.
