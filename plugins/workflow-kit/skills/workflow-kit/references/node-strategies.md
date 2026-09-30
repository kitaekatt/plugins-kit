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

One non-Claude model call via llm-scripting-kit's completion seam
(`scripts/openrouter_run.py`, which uses
`llm_scripting_kit.completion.OpenRouterBackend.complete`), writing the reply
text to `$OUT`. The script does not build an `openai` client or call
`chat.completions.create` itself -- the seam owns the transport, response
normalization, and halt classification (auth / rate limit / insufficient
credit); the script hands the node's model declaration to llm-scripting-kit's
declaration API (`llm_scripting_kit.declaration.run`) and reports the result. workflow-kit reuses `llm_scripting_kit` (owned by
the llm-scripting-kit plugin, which its bootstrap.json installs automatically) and
gets the library on its venv via the bootstrap shared-libs `.pth`. The one
third-party dep the call needs, `openai`, IS declared by workflow-kit (its own
`pyproject.toml`) -- the seam uses it internally; workflow-kit shares the source,
not the SDK.

When `--temperature` is omitted, the node leaves it unset on
`llm_scripting_kit.completion.BackendOptions` (`temperature: Optional[float] =
None`) so the provider's own mode-dependent default applies. When
`--max-tokens` is omitted, the seam's `BackendOptions` default of `4096`
applies. Pass either flag to override its value.

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
  // model omitted + cheap:true -> the default entry's 'defaultCheap' model.
  // (Or pass model: 'or-qwen', a transport entry id, or 'or-qwen,or-gpt-mini'
  // to declare more than one. Omit cheap to use the entry's 'default'.)
  cheap: true,
  promptFile: req,
  system: 'You are a terse classifier.',
  out,
  status: `./.workflow-kit/${args.runId}/gpt.status.json`,
}, { label: 'classify', phase: 'Classify' })
```

### Choosing the model

Don't hardcode slugs. llm-scripting-kit owns a model **registry** in its
`config.yaml`, resolved through bootstrap's layered config (shipped baseline,
then the user file, then a per-project override; project wins). A node names
registry ENTRIES -- a model declaration, in the one format bootstrap's
plugin-dev `references/model-declaration.md` specifies. `wkOpenRouter`'s `spec`:

- `model: 'or-qwen'` -- one transport entry. The OpenRouter models ship as
  `or-qwen`, `or-gpt-mini` and `or-gemini-lite`; any OpenAI-compatible
  transport entry you declare works the same way.
- `model: 'or-qwen,or-gpt-mini'` -- a declaration of two entries. The first
  usable one runs; a classified halt (auth, rate limit, spent credit or quota)
  moves the node to the next. An id the node cannot dispatch -- a harness entry
  such as `sol` or `opus`, or one the registry does not know -- is skipped
  without comment. When no entry is usable, the node exits 2 and its status
  file itemises every declared id and why it could not run.
- omit `model` -- use the configured default declaration (llm-scripting-kit's
  `default_endpoint`), and its `default` model, or `defaultCheap` with
  `cheap: true`. This is the usual choice: pick the *role*, not the slug.
- `model: 'qwen'` (an alias) or `model: 'qwen/qwen3-32b'` (a raw slug) -- not
  an entry id. It resolves to no entry, so the node exits 2 with the itemised
  status. Name the entry instead (`or-qwen`).

Change the entries or the defaults once, in llm-scripting-kit's config, and
every openrouter node across every plugin follows:

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
  directly, with no path discovery. workflow-kit's bootstrap.json declares
  llm-scripting-kit as a REQUIRED `install: "auto"` plugin edge, so bootstrap
  installs the owning plugin.
- `openai` is a declared workflow-kit dependency (`pyproject.toml` +
  `venv.check_imports`), so bootstrap installs it into workflow-kit's venv.

### Execution events

An openrouter node can record what its call did as a JSONL stream of
execution events, in the `plugins-kit.execution-event/v1` format that
bootstrap's plugin-dev `references/execution-events.md` specifies. Pass
`events`, `runId` and `unitId` in `wkOpenRouter`'s `spec`; the runner gets
`--events <path> --run-id <id> --unit-id <id>`. `--events` without both ids
is a usage error (exit 2).

- Every event has `source.plugin` `workflow-kit` and the given run and unit
  ids. Per attempt, the stream holds `dispatch-selected`, `call-started`,
  `usage` (when the provider reported token counts) and `result`. One
  `terminal` closes the node. llm-scripting-kit emits these through
  `run(..., observer=...)`.
- The file records the node's LAST execution: a re-run replaces it, as it
  replaces `$OUT`.
- The runner creates the file's parent directory, so a fresh run with no
  `./.workflow-kit/<runId>/` directory works.
- Before any model call, the runner checks that bootstrap_lib has
  `execution_event` with the v1 schema and the `Emitter`/`JsonlSink` calls it
  makes, and that `declaration.run` takes the `observer` keyword. An absent or
  too-old library exits 2 with the command that repairs it, and creates no
  file or directory.

A compiled `.workflow.yaml` sets all three for every openrouter node (see
`workflow-yaml.md`). A hand-written script that omits `events` records none.

A node that also PROVIDES a typed artifact (see "Typed artifacts") records
its judgment in its stream as one `contract` event, and writes the whole
stream in the `plugins-kit.execution-event/v3` format (v3 accepts every v1
event unchanged). A node that provides nothing keeps the v1 stream.

- The `contract` payload is `artifact`, `kind`, `verdict`, `schema_digest`
  (kind `schema` only) and `error_count` (verdict `violated` only). It never
  holds a payload value, an error pointer or free text; those stay in the
  verdict file.
- The `contract` event always comes before the node's `terminal`. If the call
  never started (for example, the default declaration did not resolve), the
  stream holds only the `contract` event. If no entry was usable, it holds
  `contract` (`missing`) and then `terminal` `unroutable`.
- Each execution that passes its checks attempts exactly one `contract`
  event. If the events file cannot be written, the event is not in the
  stream, the error goes to stderr, and the node exits non-zero.
- For each execution whose arguments parse, the previous events file is
  removed together with the previous verdict, before any check. A refused
  execution (exit 2) therefore leaves no stream. A node that provides
  nothing does not remove it.
- The check before the call asks bootstrap_lib for v3. A bootstrap that
  supports v1 but not v3 gets its own message, which names bootstrap 0.137.0.
- A script provider records the same `contract` event: give
  `scripts/check_artifact.py` the flags `--events <path> --run-id <id>
  --unit-id <id>` (with `wkScriptProvided`, set `events`, `runId` and
  `unitId` in its `check`). Its stream holds only that event and no
  `terminal`, because the checker judges the artifact but does not run the
  node's command. A compiled `.workflow.yaml` uses the openrouter node's path
  and unit id: `<step>[.<i>].events.jsonl`, unit `<step>` (`<step>-<i>` under
  `for_each`).

## Typed artifacts

A node can PROVIDE a named artifact: its `$OUT`, checked against a declared
type, with the judgment recorded in a verdict file. The executor's
`exit_code` and `$STATUS` are metadata about the command; the verdict file is
the proof that the payload has the declared type.

An artifact has one of two kinds:

- `schema` -- `$OUT` holds JSON that conforms to a JSON Schema. The schema must
  be inside the closed subset llm-scripting-kit's `OutputContract` accepts
  (policy `validated-result`): keywords such as `pattern`, `format` and
  `oneOf` are refused, and the root must refuse `null`. It travels as JSON
  text with its digest: the sha256 of its canonical JSON (sorted keys, no
  spaces, ASCII), the value `OutputContract(...).schema_digest` reports.
  Compute it once with workflow-kit's venv python:

  ```sh
  "<workflow-kit-venv-python>" -c "import json,sys; from llm_scripting_kit.completion import OutputContract; print(OutputContract(id='x', policy='validated-result', schema=json.load(open(sys.argv[1]))).schema_digest)" stats.schema.json
  ```

  A digest that does not match the schema means the schema changed in
  transit; the node refuses it (exit 2) before any model call or check,
  after removing the previous verdict (see "The verdict file").
- `opaque-file` -- `$OUT` is any regular file. Nothing inside it is checked.

The helpers in `references/preamble-contracts.js` build these provider
commands for you: `wkProviderFlags(check)` for an openrouter runner prefix,
`wkScriptProvided(command, out, check, opts)` for a script provider, and
`wkProvided(result, step, artifact, verdict)`, which throws when a provider
(or any fan-out item) exited non-zero so no consumer runs. A hand-written
script pastes `preamble-contracts.js` after `preamble.js`; it calls only `shq`
and `wkNode` from there. A compiled `.workflow.yaml` inlines it automatically
when a step declares `provides` (see `workflow-yaml.md`, "Typed artifacts").
The examples below spell the same commands out by hand.

### openrouter providers

Add the provider flags to the runner prefix; the order of flags does not
matter:

```js
const verdict = `./.workflow-kit/${args.runId}/classify.contract.json`
const typed = runner +
  ' --provides doc_class --kind schema --verdict ' + shq(verdict) +
  ' --schema ' + shq(STATS_SCHEMA_JSON) + ' --schema-digest ' + shq(STATS_DIGEST)
const r = await wkOpenRouter(typed, { promptFile: req, out, cheap: true }, { label: 'classify' })
```

`--provides NAME` requires `--kind` and `--verdict`; `--kind schema` also
requires `--schema` and `--schema-digest`, which `--kind opaque-file`
refuses. Any other combination is a usage error (exit 2).

- `--kind schema`: the runner sends the schema to llm-scripting-kit as an
  output contract. The seam uses only entries that can satisfy it, adds the
  schema instruction to the system message, and validates the answer. On
  success `$OUT` holds the validated value serialized as ASCII JSON, not the
  raw reply.
  An answer that is not JSON or does not conform is `violated`: exit 1 and
  `$OUT` is not written.
- `--kind opaque-file`: no contract is sent; `$OUT` is the reply text.
- Any other failure (a failed call, no usable entry, an unexpected error) is
  `missing`, with the node's usual exit code.

### script providers

Run the command in a subshell, capture its exit status on the next line, and
always run `scripts/check_artifact.py` after it. The checker's exit code is
the node's:

```js
const checker = `"${venvPy}" "${args.pluginRoot}/scripts/check_artifact.py"`
const out = `./.workflow-kit/${args.runId}/count.out`
const verdict = `./.workflow-kit/${args.runId}/count.contract.json`
const cmd = [
  'wk_e=; case $- in *e*) wk_e=1; set +e;; esac',
  '( if [ -n "$wk_e" ]; then set -e; fi',
  `"${venvPy}" wc.py "${args.source}"`,
  ') > ' + shq(out),
  'wk_rc=$?',
  'if [ -n "$wk_e" ]; then set -e; fi',
  checker + ' --artifact doc_stats --kind schema --in ' + shq(out) +
    ' --verdict ' + shq(verdict) + ' --schema ' + shq(STATS_SCHEMA_JSON) +
    ' --schema-digest ' + shq(STATS_DIGEST) + ' --command-exit "$wk_rc"',
].join('\n')
const r = await wkNode(cmd, out, { label: 'count' })
if (r.exit_code !== 0) throw new Error(`count did not provide doc_stats; see ${verdict}`)
```

The subshell keeps an `exit N` in the command from ending the shell before
the checker runs. The first and last lines suspend an outer `set -e` only
around the capture, so the checker always runs; a `set -e` inside the
command still applies. Keep the command on lines of its own, so a trailing
`#` comment cannot swallow the `)`.

Checker flags: `--artifact NAME --kind {schema,opaque-file} --in PATH
--verdict PATH --command-exit CODE [--schema JSON --schema-digest HEX]
[--events PATH --run-id ID --unit-id ID]`, with the same pairing rules as the
runner (`--events` requires both ids; see "Execution events" for the
`contract` event it records). Judgment:

- a non-zero `--command-exit` is `missing`, and the checker exits with that
  code (clamped to 1..255);
- `opaque-file` is `satisfied` when `--in` is a regular file, else `missing`;
- `schema` is `missing` when `--in` is not a readable regular file. The bytes
  must be strict UTF-8 JSON (`NaN` and `Infinity` refused), else `violated`
  with the one error `["", "unparseable"]`. A parsed value is validated with
  llm-scripting-kit's `completion.json_schema.validate`.

Checker exit codes: 0 `satisfied`; 1 `violated`, or `missing` after a command
that exited 0; the command's own code when it failed; 2 a usage error, an
absent or too-old llm-scripting-kit, a schema outside the subset, a digest
mismatch, a previous verdict or events file that cannot be removed, or (with
`--events`) a bootstrap_lib without execution-event schema v3.

### The verdict file

Both runners write the same file, `workflow-kit.artifact-verdict/v1`, one per
provided artifact per execution. Use the run directory:
`./.workflow-kit/<runId>/<step>[.<i>].contract.json`.

```json
{"schema": "workflow-kit.artifact-verdict/v1",
 "artifact": "doc_stats", "kind": "schema", "verdict": "violated",
 "path": "./.workflow-kit/r1/count.out", "bytes": 41,
 "sha256": "<hex of the bytes judged>", "schema_digest": "<hex>",
 "errors": [["/words", "type"]], "errors_truncated": false}
```

- `verdict` is `satisfied`, `violated` or `missing`.
- `bytes` and `sha256` describe exactly the bytes at `path` that were judged
  (for an openrouter provider, the bytes it wrote to `$OUT`). They are `null`
  when no bytes were judged or written: a missing file, a failed command, a
  `violated` openrouter answer. Compare them with the file you read to detect
  a file that changed after the check, for example after a resume.
- `errors` holds at most 100 `[json_pointer, keyword]` pairs and never a
  payload value; `errors_truncated` is true when there were more.
  `schema_digest` is present only for kind `schema`.
- For every execution whose arguments parse, the previous verdict is removed
  first, before any check. A refused execution (exit 2) therefore leaves no
  verdict, never an earlier `satisfied` one. If the old verdict cannot be
  removed, the node exits 2 and does nothing else. An argument error is
  reported before this step, so it can leave an earlier verdict in place.
- Every execution that passes its checks writes a verdict, on every path,
  including an unexpected error. The write is atomic (a uniquely named temp
  file, then a rename), so a verdict is never partial. If it cannot be
  written, the error is printed and a node that otherwise succeeded exits 1;
  a node that failed keeps its own exit code.
- One writer per verdict path: `runId` must be unique among runs in flight in
  one project directory. Two concurrent executions of the same node are not
  supported; the last complete write wins.

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
