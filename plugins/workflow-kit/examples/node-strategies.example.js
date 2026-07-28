// ILLUSTRATIVE workflow-kit node-strategy example (a NATIVE Workflow script).
//
// NOT run by the test suite and NOT a .workflow.yaml -- it shows the hand-written
// pattern: inline the preamble, run a deterministic `script` node (wordcount.py)
// and a non-Claude `openrouter` node (via llm-scripting-kit's openai runner), each
// writing to a file, then have ONE Claude reasoning node read the files. Payloads
// travel by file ($OUT); only the final node pays the token cost of reading them.
//
// To use: paste skills/workflow-kit/references/preamble.js where indicated and pass args:
//   { runId, pluginRoot, workflowKitVenvPython, source }
// - pluginRoot            = ${CLAUDE_PLUGIN_ROOT} for workflow-kit
// - workflowKitVenvPython = ~/.claude/plugins/data/plugins-kit/workflow-kit/.venv/
//                           {Scripts/python.exe|bin/python} (has openai + llm_scripting_kit)
// - source                = path to the input document

export const meta = {
  name: 'node-strategies-demo',
  description: 'Demo: a script node (wordcount) + an openrouter node feed a Claude reasoning node, all via file-passing.',
  phases: [{ title: 'Prepare' }, { title: 'Classify' }, { title: 'Reason' }],
}

// <<< paste skills/workflow-kit/references/preamble.js here (wkNode / wkScript / wkOpenRouter) >>>

const dir = `./.workflow-kit/${args.runId}`
const wordcount = `${args.pluginRoot}/examples/scripts/wordcount.py`
const runner = `"${args.workflowKitVenvPython}" "${args.pluginRoot}/scripts/openrouter_run.py"`

phase('Prepare')
// script strategy: deterministic stats, no LLM in the loop. stdout -> $OUT.
// Use the plugin-venv python (not bare `python`): deps resolve from any cwd and
// it dodges the Windows Store python stub. See node-strategies.md "script strategy".
const stats = await wkScript(
  `"${args.workflowKitVenvPython}" "${wordcount}" "${args.source}"`,
  `${dir}/stats.json`,
  { label: 'wordcount', phase: 'Prepare' },
)
log(`wordcount exit=${stats.exit_code} bytes=${stats.bytes}`)

phase('Classify')
// openrouter strategy: a non-Claude model call (model diversity). Reply -> $OUT.
// model omitted + cheap:true -> llm-scripting-kit's configured 'defaultCheap' model
// (set it in llm-scripting-kit's config.yaml; or pass model: 'qwen' / a raw slug).
const gpt = await wkOpenRouter(runner, {
  cheap: true,
  promptFile: args.source,
  system: 'In one word, classify the document type.',
  out: `${dir}/gpt.txt`,
  status: `${dir}/gpt.status.json`,
}, { label: 'classify', phase: 'Classify' })

phase('Reason')
// the ONLY node that reads the payloads -- token cost paid once, here.
const summary = await agent(
  `Read ${stats.path} (wordcount stats) and ${gpt.path} (external classifier verdict). ` +
  `In 3 bullets, reconcile them.`,
  { label: 'reconcile', phase: 'Reason' },
)

return { stats: stats.path, classifier: gpt.path, summary }
