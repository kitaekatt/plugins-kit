# Node strategies: script and openrouter

A *node strategy* is a recipe for building a contract-fulfilling shell command
(see `contract.md`) that the `workflow-kit-agent` executor runs. There is ONE
executor agent; strategies differ only in the command they generate. New
strategies are new command templates -- no new agent.

Two ways to use them. **Declaratively** (preferred for humans): a `.workflow.yaml`
`script:` or `openrouter:` step -- the compiler inlines the preamble and emits the
call for you (see `workflow-yaml.md`). **By hand**: inline `preamble.js` into a
native Workflow script (scripts are sandboxed and cannot `import`, so the helpers
are pasted in) and call `wkScript(...)` / `wkOpenRouter(...)` -- shown below.

## The executor

`workflow-kit-agent` (shipped at `agents/workflow-kit-agent.md`, resolves as
`workflow-kit:workflow-kit-agent`) runs on **haiku**, has only `Bash`, and is a
verbatim command runner: it runs the command, never reads `$OUT`, and returns
the node metadata. It is generically named because its capabilities may grow
beyond shell execution.

## script strategy

Run any deterministic shell command (a Python script, a CLI tool, a transform),
redirecting stdout to `$OUT`. Use the plugin-venv interpreter for Python so deps
resolve from any cwd (see SKILL.md for the path).

```js
const out = `./.workflow-kit/${args.runId}/parsed.json`
const r = await wkScript(
  `"${venvPython}" -m mypkg.parse "${args.source}"`,
  out,
  { label: 'parse', phase: 'Prepare' },
)
// r = { exit_code, path: out, bytes, sha256, status }
if (r.exit_code !== 0) { /* route to a failure path */ }
```

The command's stdout becomes the payload at `out`; stderr surfaces via the exit
code. Anything `bash -c` can run is a script node.

## openrouter strategy

One non-Claude model call via llm-scripting-kit's openai runner
(`scripts/openrouter_run.py`, which uses `llm_scripting_kit.make_openai_client`),
writing the reply text to `$OUT`. workflow-kit reuses `llm_scripting_kit` (owned by
the llm-scripting-kit plugin) WITHOUT declaring a dependency on that plugin -- it
gets the library on its venv via the bootstrap shared-libs `.pth`. The one
third-party dep the call needs, `openai`, IS declared by workflow-kit (its own
`pyproject.toml`).

Run the runner with **workflow-kit's own venv python** -- bootstrap provisions it
with `openai` (declared) and links `llm_scripting_kit` onto it (declared via
`shared_lib_imports`). The API key is resolved the llm-scripting-kit way; run
`llm-scripting-kit set-key` once to provision it.

```js
// workflow-kit's venv python: has openai + llm_scripting_kit (shared-libs .pth).
const venvPy = args.workflowKitVenvPython
//   ~/.claude/plugins/data/plugins-kit/workflow-kit/.venv/Scripts/python.exe  (Windows)
//   ~/.claude/plugins/data/plugins-kit/workflow-kit/.venv/bin/python          (macOS/Linux)
const runner = `"${venvPy}" "${args.pluginRoot}/scripts/openrouter_run.py"`

const req = `./.workflow-kit/${args.runId}/req.txt`  // prompt written first (a script node or upstream)
const out = `./.workflow-kit/${args.runId}/gpt.txt`
const r = await wkOpenRouter(runner, {
  // model omitted + cheap:true -> llm-scripting-kit's configured 'defaultCheap'.
  // (Or pass model: 'qwen' for a registry alias, or a raw slug like
  // 'qwen/qwen3-32b'. Omit cheap to use the configured 'default'.)
  cheap: true,
  promptFile: req,
  system: 'You are a terse classifier.',
  out,
  status: `./.workflow-kit/${args.runId}/gpt.status.json`,
}, { label: 'classify', phase: 'Classify' })
```

### Choosing the model

Don't hardcode slugs. llm-scripting-kit owns a model **registry** in its
`config.yaml` -- named models plus a `default` and a `defaultCheap` selector --
resolved through bootstrap's layered config (shipped baseline, then the user
file, then a per-project override; project wins). `wkOpenRouter`'s `spec`:

- `model: 'qwen'` -- a registry alias, resolved to its slug.
- `model: 'qwen/qwen3-32b'` -- a raw OpenRouter slug, used as-is.
- omit `model` -- use the configured `default` (or `defaultCheap` with
  `cheap: true`). This is the usual choice: pick the *role*, not the slug.

Change the models or the default/defaultCheap once, in llm-scripting-kit's config,
and every openrouter node across every plugin follows:

- user (all projects): `~/.claude/plugins/data/plugins-kit/llm-scripting-kit/config.yaml`
- per-project override: `<project_root>/.local-data/plugins-kit/llm-scripting-kit/config.yaml`

Use it for model diversity (a non-Claude judge in a panel) or a cheaper/faster
model for bulk work. Cost shape: you pay haiku tokens for the executor shim PLUS
the OpenRouter call -- the cost-savings case is weaker than the model-diversity
case (the reply still bypasses context via `$OUT`).

Provisioning:
- `llm_scripting_kit` (owned by the llm-scripting-kit plugin) is published as a
  shared library by the bootstrap engine and linked onto workflow-kit's venv because workflow-kit
  declares `"shared_lib_imports": ["llm_scripting_kit"]` -- the runner imports it
  directly, no path discovery, no dependency on the llm-scripting-kit plugin.
- `openai` is a declared workflow-kit dependency (`pyproject.toml` +
  `venv.check_imports`), so bootstrap installs it into workflow-kit's venv.

## Consuming a node's output

A downstream Claude reasoning node reads the payload only when it must reason
over it -- that is where the token cost is paid, once:

```js
const verdict = await agent(
  `Read ${r.path} and summarize the three biggest risks it lists.`,
  { label: 'summarize', phase: 'Reason', schema: SUMMARY_SCHEMA },
)
```

Until then the payload never enters any context. Route earlier nodes on
`r.exit_code` and `r.status`, not on the file body.

## When NOT to use a node strategy

- Bit-exact determinism required -> a haiku agent is in the loop; do it in the
  main loop instead.
- Large data with no downstream LLM consumer -> keep it out of the graph; the
  node only earns its place when a later node is data-dependent on it.
- A one-off deterministic prep step before the workflow -> run it in the main
  loop and pass results via `args`.
