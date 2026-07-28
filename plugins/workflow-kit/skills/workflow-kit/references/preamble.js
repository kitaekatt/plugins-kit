// workflow-kit preamble -- paste at the top of a native Workflow script to use
// workflow-kit node strategies (script / openrouter). Every helper compiles down
// to a single agent() call against the workflow-kit-agent executor; the node's
// payload is written to a file ($OUT) and never enters the model context.
//
// ASCII-only. Contains no nondeterministic-time / random calls (those are banned
// inside Workflow scripts -- they break resume). Vary per-node behavior by index
// or by values passed in via `args`, not by random.

// Shell-quote one argument for POSIX sh: wrap in single quotes, escaping any
// embedded single quote with the '\'' dance. Single quotes stop EVERY expansion
// ($var, $(cmd), backticks); double quotes do not, and JSON.stringify is a JS
// escaper, not a shell escaper. Use this for every value spliced into a command.
function shq(s) {
  return "'" + String(s).replace(/'/g, "'\\''") + "'"
}

const WORKFLOW_KIT_NODE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    exit_code: { type: 'integer' },
    path: { type: 'string' },
    bytes: { type: 'integer' },
    sha256: { type: 'string' },
    status: { type: ['object', 'null'] },
  },
  required: ['exit_code', 'path', 'bytes'],
}

// Run one shell command via the workflow-kit-agent executor. `cmd` MUST write
// its primary output to `out` -- the strategy helpers below build conformant
// commands. Returns the executor's metadata object (see the schema above);
// the payload is on disk at `out`, not in this result.
function wkNode(cmd, out, opts) {
  opts = opts || {}
  const prompt =
    'Run this workflow-kit node.\n' +
    'OUT=' + out + '\n' +
    (opts.status ? 'STATUS=' + opts.status + '\n' : '') +
    'COMMAND:\n' + cmd + '\n'
  const agentOpts = {
    agentType: 'workflow-kit:workflow-kit-agent',
    model: 'haiku',
    schema: WORKFLOW_KIT_NODE_SCHEMA,
  }
  if (opts.label) agentOpts.label = opts.label
  if (opts.phase) agentOpts.phase = opts.phase
  return agent(prompt, agentOpts)
}

// script strategy: run any shell command, redirecting its stdout to `out`.
// stderr is left to surface via the exit code. `command` is passed through as-is
// (it may contain its own shell syntax -- pipes, redirects); only the `out` path
// is shq-quoted, so a path with spaces, `$`, or backticks cannot split or expand
// inside the redirect. Example:
//   await wkScript('uv run python -m mypkg.transform in.json', outPath, { label: 'transform' })
function wkScript(command, out, opts) {
  return wkNode('{ ' + command + ' ; } > ' + shq(out), out, opts)
}

// openrouter strategy: one non-Claude model call via llm-scripting-kit's openai
// runner (scripts/openrouter_run.py -> llm_scripting_kit.make_openai_client),
// written to `out`. `runner` is the command prefix that runs that script under
// workflow-kit's OWN venv python -- it declares `openai` (its pyproject) and gets
// `llm_scripting_kit` on its path via the bootstrap shared-libs .pth, e.g.
//   '"<workflow-kit-venv-python>" "<pluginRoot>/scripts/openrouter_run.py"'
// where <workflow-kit-venv-python> is
//   ~/.claude/plugins/data/plugins-kit/workflow-kit/.venv/Scripts/python.exe  (Windows)
//   ~/.claude/plugins/data/plugins-kit/workflow-kit/.venv/bin/python          (macOS/Linux)
// `spec` = { model?, cheap?, promptFile, system?, out, status? }. `model` may be
// a registry alias (e.g. 'qwen') or a raw slug (e.g. 'qwen/qwen3-32b'). When
// `model` is omitted the runner uses llm-scripting-kit's configured 'default'
// (or 'defaultCheap' when `cheap` is true) -- configure those in llm-scripting-kit's
// config.yaml instead of hardcoding a slug here.
function wkOpenRouter(runner, spec, opts) {
  opts = opts || {}
  // every spec value is shq-quoted: paths may contain spaces, and only single
  // quotes stop $(), backtick, and $var expansion (--system in particular is
  // arbitrary multi-line prose).
  const model = spec.model ? ' --model ' + shq(spec.model) : ''
  const cheap = spec.cheap ? ' --cheap' : ''
  const sys = spec.system ? ' --system ' + shq(spec.system) : ''
  const st = spec.status ? ' --status ' + shq(spec.status) : ''
  const cmd =
    runner + model + cheap +
    ' --prompt-file ' + shq(spec.promptFile) +
    ' --out ' + shq(spec.out) + sys + st
  const merged = { status: spec.status }
  if (opts.label) merged.label = opts.label
  if (opts.phase) merged.phase = opts.phase
  return wkNode(cmd, spec.out, merged)
}
