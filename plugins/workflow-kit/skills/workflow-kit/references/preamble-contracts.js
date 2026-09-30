// workflow-kit typed-contract preamble -- paste AFTER preamble.js into a native
// Workflow script whose script or openrouter nodes PROVIDE a named artifact.
// The compiler inlines it only when a step declares `provides`. It calls only
// `shq` and `wkNode` from preamble.js and redefines nothing there.
//
// ASCII-only. Contains no nondeterministic-time / random calls (those are banned
// inside Workflow scripts -- they break resume).
//
// A provider `check` object:
//   { runner?, artifact, kind, schema?, digest?, verdict }
// - `runner` (script providers only) runs scripts/check_artifact.py under
//   workflow-kit's venv python, e.g.
//   '"<workflow-kit-venv-python>" "<pluginRoot>/scripts/check_artifact.py"'
// - `kind` is 'schema' or 'opaque-file'; 'schema' also takes `schema` (the
//   schema's canonical JSON text) and `digest` (its sha256, the value
//   llm-scripting-kit's OutputContract(...).schema_digest reports).
// - `verdict` is where the node writes its workflow-kit.artifact-verdict/v1
//   file, e.g. `./.workflow-kit/${runId}/<step>[.<i>].contract.json`.

// The provider flags an openrouter node's runner takes. Append them to the
// runner prefix passed to the unchanged wkOpenRouter (argparse accepts the
// flags in any order):
//   await wkOpenRouter(runner + wkProviderFlags(check), spec, opts)
function wkProviderFlags(check) {
  let flags = ' --provides ' + shq(check.artifact) + ' --kind ' + shq(check.kind)
  if (check.kind === 'schema') {
    flags += ' --schema ' + shq(check.schema) + ' --schema-digest ' + shq(check.digest)
  }
  return flags + ' --verdict ' + shq(check.verdict)
}

// script provider: run `command` in a subshell with stdout redirected to `out`,
// capture its exit status on the next line, then ALWAYS run the checker, whose
// exit code is the node's. The subshell contains an author's `exit N`. An outer
// `set -e` is suspended only around the capture (so the checker still runs),
// re-enabled inside the subshell before `command` (so it applies there as it
// does in wkScript), and restored afterwards. The subshell is a plain
// statement, never an if/while/&&/|| condition, so an authored `set -e` inside
// `command` stays in force. `command` sits on lines of its own, so a trailing
// `#` comment in it cannot swallow the `)`. POSIX sh only (bash 3.2, zsh).
function wkScriptProvided(command, out, check, opts) {
  let schemaFlags = ''
  if (check.kind === 'schema') {
    schemaFlags = ' --schema ' + shq(check.schema) + ' --schema-digest ' + shq(check.digest)
  }
  const cmd = [
    'wk_e=; case $- in *e*) wk_e=1; set +e;; esac',
    '( if [ -n "$wk_e" ]; then set -e; fi',
    command,
    ') > ' + shq(out),
    'wk_rc=$?',
    'if [ -n "$wk_e" ]; then set -e; fi',
    check.runner +
      ' --artifact ' + shq(check.artifact) +
      ' --kind ' + shq(check.kind) +
      ' --in ' + shq(out) +
      ' --verdict ' + shq(check.verdict) +
      schemaFlags +
      ' --command-exit "$wk_rc"',
  ].join('\n')
  return wkNode(cmd, out, opts)
}

// The provider guard: call it right after a provider node's result arrives.
// Throws when the result -- or, for a fan-out, any item of the result array --
// is not an object or reports a non-zero exit_code, so no later step (no
// consumer of the artifact) runs. `verdict` is the verdict path, or for a
// fan-out a function of the item index returning it.
function wkProvided(result, step, artifact, verdict) {
  const items = Array.isArray(result) ? result : [result]
  const fanout = Array.isArray(result)
  for (let i = 0; i < items.length; i++) {
    const r = items[i]
    const ok = r !== null && typeof r === 'object' && r.exit_code === 0
    if (!ok) {
      const code = r !== null && typeof r === 'object' ? r.exit_code : 'none'
      const where = typeof verdict === 'function' ? verdict(i) : verdict
      throw new Error(
        'workflow-kit: step ' + step + ' did not provide artifact ' + artifact +
        ' (' + (fanout ? 'item ' + i + ', ' : '') + 'exit_code ' + code + '); see ' + where
      )
    }
  }
  return result
}
