#!/usr/bin/env node
'use strict'
// Print the exact instantiated lane prompt(s) a md-domain detect Workflow script
// would send to agent(), for a caller that cannot use the Workflow tool (a
// background runner is a subagent, and a subagent has no Workflow tool).
//
// The prompt text lives ONLY in workflow/<lane>-detect.js. This helper does not
// copy or re-derive it: it runs the real shipped script with the same `args` the
// Workflow tool would hand it, against a stub agent() that records the prompt and
// schema it is called with and returns nothing. Nothing is dispatched.
//
// Usage:
//   node lane-prompt.cjs --lane claude-md|skill|project-doc --args-file <args.json> [--out-dir <dir>]
//   node lane-prompt.cjs --lane <lane> < args.json            (args on stdin)
//
// --args-file / stdin carry the detect script's `args` object as JSON, byte for
// byte what the Workflow tool would receive (files, review, disabledCriteria,
// laneModels, mechanicalCheckPhrases, refs).
//
// stdout is one JSON object:
//   { lane, script, run, lanes: [ { index, file, label, lanePromptLength, dispatchPrompt | promptFile } ] }
// `dispatchPrompt` is what to send as an Agent tool's `prompt`: the script's
// lanePrompt, unchanged, as its first `lanePromptLength` characters, followed by
// the script's exact FILE_FINDINGS_SCHEMA and the instruction to return one JSON
// object matching it. `run` is args.laneModels.run, the ordered {id, effort}
// entries to dispatch with. With --out-dir, each dispatchPrompt is written to
// <dir>/<index>.prompt.txt instead and stdout carries only that absolute path as
// `promptFile`, so a prompt of tens of kilobytes is not printed, and a caller can
// have the lane read the file instead of pasting the text. Any failure -- bad args, a script that throws, a lane count that does not
// match the files -- exits 1 with the reason on stderr and prints no JSON.

const fs = require('fs')
const path = require('path')

const LANES = {
  'claude-md': 'claude-md-detect.js',
  'skill': 'skill-detect.js',
  'project-doc': 'project-doc-detect.js',
}

function fail(msg) {
  process.stderr.write('lane-prompt: ' + msg + '\n')
  process.exit(1)
}

function parseCli(argv) {
  const opts = {}
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (a === '--lane' || a === '--args-file' || a === '--out-dir') {
      if (i + 1 >= argv.length) fail(a + ' needs a value')
      opts[a.slice(2)] = argv[++i]
    } else {
      fail('unknown argument ' + JSON.stringify(a) + '; usage: --lane ' + Object.keys(LANES).join('|') + ' [--args-file <json>] [--out-dir <dir>]')
    }
  }
  if (!opts.lane) fail('--lane is required (' + Object.keys(LANES).join(', ') + ')')
  if (!Object.prototype.hasOwnProperty.call(LANES, opts.lane)) {
    fail('unknown lane ' + JSON.stringify(opts.lane) + '; expected one of ' + Object.keys(LANES).join(', '))
  }
  return opts
}

function readArgs(opts) {
  let text
  try {
    text = opts['args-file'] ? fs.readFileSync(opts['args-file'], 'utf8') : fs.readFileSync(0, 'utf8')
  } catch (e) {
    fail('cannot read args: ' + e.message)
  }
  try {
    return JSON.parse(text)
  } catch (e) {
    fail('args are not valid JSON: ' + e.message)
  }
}

// Run the shipped detect script exactly as the Workflow tool does (an async
// function body over `args, agent, parallel, phase, log`) and return what its
// agent() calls received. agent() returns null, which the script's laneAgent
// treats as a failed entry and retries on the next run entry, so the same prompt
// is seen once per entry; the first sighting of each prompt is kept.
async function capture(scriptPath, args) {
  const src = fs.readFileSync(scriptPath, 'utf8').replace('export const meta', 'const meta')
  const seen = []
  const agent = (prompt, opts) => {
    if (!seen.some((s) => s.prompt === prompt)) {
      seen.push({ label: opts && opts.label, prompt, schema: opts && opts.schema })
    }
    return Promise.resolve(null)
  }
  const parallel = (thunks) => Promise.all(thunks.map((t) => Promise.resolve().then(t)))
  const fn = new Function('args', 'agent', 'parallel', 'phase', 'log',
    '"use strict"; return (async () => {\n' + src + '\n})()')
  await fn(args, agent, parallel, () => {}, () => {})
  return seen
}

function dispatchPrompt(prompt, schema) {
  return prompt + '\n\nReturn ONLY one JSON object that matches this JSON Schema exactly -- no prose, no code fence.\n' +
    'Schema (FILE_FINDINGS_SCHEMA):\n' + JSON.stringify(schema)
}

async function main() {
  const opts = parseCli(process.argv.slice(2))
  const args = readArgs(opts)
  const scriptName = LANES[opts.lane]
  const scriptPath = path.join(__dirname, '..', 'workflow', scriptName)
  const files = args && (typeof args === 'string' ? null : args.files)
  const seen = await capture(scriptPath, args)
  if (!Array.isArray(files) || seen.length !== files.length) {
    fail(scriptName + ' dispatched ' + seen.length + ' distinct lane prompt(s) for ' +
      (Array.isArray(files) ? files.length : 'an unknown number of') + ' file(s); expected one each')
  }
  if (opts['out-dir']) fs.mkdirSync(opts['out-dir'], { recursive: true })
  const lanes = seen.map((s, index) => {
    if (typeof s.prompt !== 'string' || s.prompt === '' || !s.schema) {
      fail(scriptName + ' lane ' + index + ' dispatched without a prompt or schema')
    }
    const text = dispatchPrompt(s.prompt, s.schema)
    const lane = { index, file: files[index].path, label: s.label, lanePromptLength: s.prompt.length }
    if (opts['out-dir']) {
      lane.promptFile = path.resolve(opts['out-dir'], index + '.prompt.txt')
      fs.writeFileSync(lane.promptFile, text, 'utf8')
    } else {
      lane.dispatchPrompt = text
    }
    return lane
  })
  process.stdout.write(JSON.stringify({ lane: opts.lane, script: scriptName, run: args.laneModels.run, lanes }) + '\n')
}

main().catch((e) => fail(String((e && e.message) || e)))
