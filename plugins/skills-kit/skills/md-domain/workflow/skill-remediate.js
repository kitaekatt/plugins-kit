// md-domain audit_skill lane — REMEDIATE workflow (after-Q&A phase).
//
// Fan-out remediation, one lane per SKILL.md, applying the decisions the main
// loop gathered during the Q&A gate (interactive) or inferred (non-interactive).
// Runs AFTER detection + the user decision step — never folded into detection
// (the `audit_then_self_remediate` anti-pattern keeps the two phases apart so
// re-running the audit reproduces the same findings).
//
// One lane per FILE (not per finding) so two lanes never edit the same file
// concurrently; within a lane, remediations are applied in order. No worktree
// isolation: lanes touch disjoint files, so they cannot conflict. Note: some
// remediations also touch the skill's co-located CLAUDE.md (taxonomy I) or a
// references/*.md (taxonomy H) — those still live under the skill's own
// directory, so disjoint-skill lanes remain conflict-free.
//
// Invoked by the md-domain SKILL.md (audit_skill lane) only when there is remediation work
// spanning 2+ files (the multi-file threshold that equalizes Workflow-tool
// overhead). Single-file remediation runs inline in the main loop.
//
// args = {
//   perFile: [ {
//     path: string,
//     remediations: [ {
//       criterion: string, taxonomy: string, bucket: "FIX"|"SERIOUS"|"IMPROVE"|"SILENT"|"SPECIAL",
//       line: integer|null,
//       instruction: string,          // the concrete edit to make
//       decision: "apply"|"skip"|string  // user/inferred decision; free-text = a
//                                          // refined instruction to apply instead
//     } ]
//   } ],
//   fixMode: "apply"|"propose",  // REQUIRED: audit.fix_mode from
//                                // scripts/resolve_standards.py; absent throws
//   laneModels: { run: [{id, effort}], dropped: [{id, effort, reason}] }
//                               // REQUIRED: lane_models.remediate from
//                               // scripts/resolve_standards.py; absent throws
//   maxConcurrent: integer|undefined
//                               // OPTIONAL: a positive integer bounding the agents in
//                               // flight in each fan-out (a worker pool: a free slot
//                               // takes the next item at once); absent leaves it
//                               // unbounded apart from the runtime cap; any other
//                               // value throws before any agent runs
// }

export const meta = {
  name: 'md-domain-skill-remediate',
  description: 'Fan-out SKILL.md remediation: apply the decided edits, one lane per file (after-Q&A phase)',
  phases: [{ title: 'Remediate', detail: 'one lane per SKILL.md' }],
}

const FILE_RESULT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    path: { type: 'string' },
    applied: { type: 'integer' },
    skipped: { type: 'integer' },
    failed: { type: 'integer' },
    actions: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          criterion: { type: 'string' },
          status: { type: 'string', enum: ['applied', 'skipped', 'failed'] },
          note: { type: 'string' },
        },
        required: ['criterion', 'status', 'note'],
      },
    },
  },
  required: ['path', 'applied', 'skipped', 'failed', 'actions'],
}

let input = args
if (typeof input === 'string') {
  try { input = JSON.parse(input) } catch (_) { input = null }
}
if (!input || !Array.isArray(input.perFile) || input.perFile.length === 0) {
  throw new Error('remediate.js requires args.perFile = [{path, remediations}]')
}

// fixMode is REQUIRED. It is the resolved `audit.fix_mode` from
// scripts/resolve_standards.py, threaded by the caller. An absent or unknown
// value throws before anything else runs: reading "not passed" as "apply" would
// edit files for a consumer whose config says propose.
if (input.fixMode !== 'apply' && input.fixMode !== 'propose') {
  throw new Error(`remediate.js requires args.fixMode = "apply" | "propose", got ${JSON.stringify(input.fixMode) ?? 'nothing'}. Pass audit.fix_mode from scripts/resolve_standards.py; an absent fixMode is never read as "apply".`)
}

// lane-route: begin (shared chunk; the generator rewrites this region)
// laneModels is REQUIRED: this lane family's resolved route from
// scripts/resolve_standards.py (lane_models.<family>), threaded by the caller.
// run is the ordered list of {id, effort, route} entries, ranked by llm-kit
// describe() (a usable reset entry first); route is 'agent' (agent() runs a
// Claude core id) or 'codex' (the md-lane bridge runs a codex harness entry), and
// an entry with no route is an agent entry. dropped lists the declared entries
// that cannot run, each with its reason. An absent or malformed value throws
// before any agent is dispatched: reading "not passed" as "inherit the session
// model" is the default this argument exists to remove.
const LANE_CORE_IDS = ['fable', 'haiku', 'opus', 'sonnet']
const laneModels = input.laneModels
const laneRouteOf = (e) => (e.route === undefined ? 'agent' : e.route)
const isLaneEntry = (e) => !!e && typeof e === 'object' &&
  typeof e.id === 'string' && e.id.trim() !== '' &&
  typeof e.effort === 'string' && e.effort.trim() !== '' &&
  (laneRouteOf(e) === 'agent' || laneRouteOf(e) === 'codex')
if (!laneModels || typeof laneModels !== 'object' || !Array.isArray(laneModels.run) ||
    laneModels.run.length === 0 || !laneModels.run.every(isLaneEntry) || !Array.isArray(laneModels.dropped)) {
  throw new Error(`md-domain lane requires args.laneModels = {run: [{id, effort, route}, ...], dropped: [...]} with a non-empty run list and route "agent" or "codex", got ${JSON.stringify(laneModels) ?? 'nothing'}. Pass this lane family's lane_models entry from scripts/resolve_standards.py; an absent route is never read as "inherit the session model".`)
}
const laneUnrunnable = laneModels.run.filter((e) => laneRouteOf(e) === 'agent' && !LANE_CORE_IDS.includes(e.id))
if (laneUnrunnable.length > 0) {
  throw new Error(`md-domain lane: args.laneModels.run names ${laneUnrunnable.map((e) => JSON.stringify(e.id)).join(', ')} on the agent route, which agent() cannot run (runnable ids: ${LANE_CORE_IDS.join(', ')}). The resolver gives a codex entry the codex route and moves any other id to laneModels.dropped; a run list that still carries one was not produced by it.`)
}
const laneEntryText = (e) => String(e.id) + ' (' + String(e.effort) + ')'
if (laneModels.dropped.length > 0) {
  log('md-domain lanes: dropped ' + laneModels.dropped.map(laneEntryText).join(', ') +
    ' -- see routes.dropped for each reason; running ' + laneModels.run.map(laneEntryText).join(', '))
}

// codexMaxConcurrent is OPTIONAL: a positive integer bounding the codex lanes in
// flight at once across this script (default LANE_CODEX_DEFAULT_CAP). A lane whose
// next entry is codex while the cap is full moves to its following entry at once;
// it does not wait. Any other value throws before any agent is dispatched.
const LANE_CODEX_DEFAULT_CAP = 4
const laneCodexArg = input.codexMaxConcurrent
if (laneCodexArg !== undefined && !(typeof laneCodexArg === 'number' && Number.isInteger(laneCodexArg) && laneCodexArg >= 1)) {
  throw new Error(`md-domain lane: args.codexMaxConcurrent must be a positive integer (the most codex lanes in flight at once) or absent, got ${JSON.stringify(laneCodexArg)}. Omit it for the default of ${LANE_CODEX_DEFAULT_CAP}.`)
}
const laneCodexCap = laneCodexArg === undefined ? LANE_CODEX_DEFAULT_CAP : laneCodexArg
let laneCodexInFlight = 0

// The md-lane bridge. A Workflow script has no shell, so a codex entry runs
// through the skills-kit:md-lane-bridge executor (a Bash-only haiku subagent at
// low effort) running scripts/run_lane.py once. The lane payload travels in a
// here-document; laneDigest over its exact text goes with it, and run_lane.py
// refuses a payload that arrives changed. The helper validates codex's answer
// against the lane schema and prints it with laneDigest(laneCanonical(result));
// the executor returns that line, and a result whose digest does not match was
// not returned verbatim and fails the entry. Both functions mirror
// skills_kit_lib/lane_bridge.py (digest, canonical) exactly.
const LANE_BRIDGE_AGENT = 'skills-kit:md-lane-bridge'
const LANE_BRIDGE_MODEL = 'haiku'
const LANE_BRIDGE_EFFORT = 'low'
const LANE_BRIDGE_EOF = 'MD_LANE_BRIDGE_INPUT'
const laneCanonical = (v) => {
  if (Array.isArray(v)) return '[' + v.map(laneCanonical).join(',') + ']'
  if (v !== null && typeof v === 'object') {
    return '{' + Object.keys(v).sort().map((k) => JSON.stringify(k) + ':' + laneCanonical(v[k])).join(',') + '}'
  }
  return JSON.stringify(v)
}
const laneDigest = (s) => {
  let h = 0x811c9dc5
  let n = 0
  for (const ch of s) {
    h = Math.imul(h ^ ch.codePointAt(0), 0x01000193) >>> 0
    n++
  }
  return 'fnv1a32:' + n + ':' + h.toString(16).padStart(8, '0')
}
function laneBridgeCommand(payloadText) {
  return '"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" ' +
    '"${SKILLS_KIT_ROOT:?requires a bootstrap engine pass; run bootstrap run}/scripts/run_lane.py" ' +
    '--input-digest ' + laneDigest(payloadText) + " <<'" + LANE_BRIDGE_EOF + "'\n" +
    payloadText + '\n' + LANE_BRIDGE_EOF
}
function laneBridgeSchema(resultSchema) {
  return {
    type: 'object',
    additionalProperties: false,
    properties: {
      exit_code: { type: 'integer' },
      status: {
        type: 'object',
        additionalProperties: false,
        properties: {
          ok: { type: 'boolean' },
          error: { type: 'string' },
          exit: { type: 'integer' },
          entry: { type: ['object', 'null'] },
          access: { type: 'string' },
          endpoint: { type: 'string' },
          served_model: { type: 'string' },
          argv: { type: 'array', items: { type: 'string' } },
          controls: { type: 'array', items: { type: 'string' } },
          digest: { type: 'string' },
          result: resultSchema,
        },
        required: ['ok'],
      },
    },
    required: ['exit_code', 'status'],
  }
}
// Run one lane on a codex entry; resolve to the helper's status, or throw the
// reason the entry failed.
async function laneBridge(e, prompt, opts, access) {
  if (!opts.schema) throw new Error('the codex route needs the lane result schema; this dispatch passed none')
  const payload = { v: 1, entry: { id: e.id, effort: e.effort }, access, prompt, schema: opts.schema }
  if (typeof input.projectRoot === 'string' && input.projectRoot !== '') payload.cwd = input.projectRoot
  const text = JSON.stringify(payload)
  const bridgeOpts = { agentType: LANE_BRIDGE_AGENT, model: LANE_BRIDGE_MODEL, effort: LANE_BRIDGE_EFFORT, schema: laneBridgeSchema(opts.schema) }
  if (opts.label !== undefined) bridgeOpts.label = opts.label
  if (opts.phase !== undefined) bridgeOpts.phase = opts.phase
  const r = await agent('Run this md-domain codex lane through the md-lane bridge.\nCOMMAND:\n' + laneBridgeCommand(text) + '\n', bridgeOpts)
  if (r === null || r === undefined) throw new Error('md-lane bridge returned nothing')
  const st = r.status
  if (!st || st.ok !== true) {
    throw new Error('codex lane failed (exit ' + String(r.exit_code) + '): ' + String((st && st.error) || 'no status'))
  }
  if (!st.entry || st.entry.id !== e.id) {
    throw new Error('md-lane bridge ran ' + JSON.stringify(st.entry) + ', not ' + e.id)
  }
  if (st.result === undefined || laneDigest(laneCanonical(st.result)) !== st.digest) {
    throw new Error('md-lane bridge result digest mismatch: the executor did not return the helper result verbatim')
  }
  return st
}

const laneRoutePerFile = []
// Dispatch one lane through the run list. Entries are tried in order; the next
// one runs when agent() or the bridge throws or returns nothing, and when a codex
// entry meets a full codex cap. opts.access is 'write' for a lane that edits
// files (generate's compose and apply steps, apply-mode remediate) and is read-only
// otherwise; it reaches only the codex sandbox. A lane whose every entry failed
// returns null, as a single agent() call that died does, and each failure stays
// on its route record.
async function laneAgent(key, prompt, opts) {
  const { access, ...agentOpts } = opts || {}
  const laneAccess = access === 'write' ? 'write' : 'read-only'
  const failed = []
  for (const e of laneModels.run) {
    if (laneRouteOf(e) === 'codex') {
      if (laneCodexInFlight >= laneCodexCap) {
        failed.push({ id: e.id, effort: e.effort, reason: 'codex lane cap reached: ' + laneCodexCap + ' codex lane(s) already in flight (args.codexMaxConcurrent)' })
        continue
      }
      laneCodexInFlight++
      let st = null
      try {
        st = await laneBridge(e, prompt, agentOpts, laneAccess)
      } catch (err) {
        failed.push({ id: e.id, effort: e.effort, reason: String((err && err.message) || err) })
        continue
      } finally {
        laneCodexInFlight--
      }
      laneRoutePerFile.push({
        path: key,
        used: { id: e.id, effort: e.effort, route: 'codex', access: laneAccess, endpoint: st.endpoint, servedModel: st.served_model, controls: st.controls || [] },
        failed,
      })
      return st.result
    }
    let r = null
    try {
      r = await agent(prompt, { ...agentOpts, model: e.id, effort: e.effort })
    } catch (err) {
      failed.push({ id: e.id, effort: e.effort, reason: String((err && err.message) || err) })
      continue
    }
    if (r !== null && r !== undefined) {
      laneRoutePerFile.push({ path: key, used: { id: e.id, effort: e.effort }, failed })
      return r
    }
    failed.push({ id: e.id, effort: e.effort, reason: 'agent() returned nothing' })
  }
  laneRoutePerFile.push({ path: key, used: null, failed })
  log('md-domain lane route exhausted for ' + key + ': ' +
    failed.map((f) => laneEntryText(f) + ': ' + f.reason).join('; '))
  return null
}
const laneRoutes = () => ({ dropped: laneModels.dropped, perFile: laneRoutePerFile })

// maxConcurrent is OPTIONAL: a positive integer bounding the agents in flight in
// each fan-out. Absent leaves the fan-out unbounded apart from the runtime cap.
// Any other value (0, negative, fractional, string, null) throws before any agent
// is dispatched, because a bound that is silently ignored is a bound that was
// never applied.
const laneMaxConcurrent = input.maxConcurrent
const laneBounded = laneMaxConcurrent !== undefined
if (laneBounded && !(typeof laneMaxConcurrent === 'number' && Number.isInteger(laneMaxConcurrent) && laneMaxConcurrent >= 1)) {
  throw new Error(`md-domain lane: args.maxConcurrent must be a positive integer (the most agents in flight per fan-out) or absent, got ${JSON.stringify(laneMaxConcurrent)}. Pass a whole number of 1 or more, or omit it for an unbounded fan-out.`)
}
if (laneBounded) {
  log('md-domain lanes: at most ' + laneMaxConcurrent + ' agent(s) in flight per fan-out (args.maxConcurrent)')
}
// Every fan-out in a lane script goes through laneParallel, never a bare
// parallel(). Unset, or no more items than the bound: parallel() itself, so the
// call behaves as it always has. Bounded: a worker pool of laneMaxConcurrent
// workers, each taking the next item the moment it finishes its last, so a free
// slot never waits on the slowest of a fixed batch. Results keep input order. An
// item that throws resolves to null, as it does under parallel(), and never stops
// the other workers.
async function laneParallel(thunks) {
  if (!laneBounded || thunks.length <= laneMaxConcurrent) return parallel(thunks)
  const results = new Array(thunks.length).fill(null)
  let next = 0
  const worker = async () => {
    while (next < thunks.length) {
      const i = next++
      try { results[i] = await thunks[i]() } catch (_) { results[i] = null }
    }
  }
  await parallel(Array.from({ length: laneMaxConcurrent }, () => worker))
  return results
}
// lane-route: end

// Drop files whose every remediation is a skip — nothing to do, no lane needed.
const actionable = input.perFile.filter(
  (f) => Array.isArray(f.remediations) && f.remediations.some((r) => r.decision !== 'skip')
)

// Propose-only guard. args.fixMode comes from resolve_standards.py's
// `audit.fix_mode`. When it is "propose" the lane makes NO edit and dispatches NO
// agent: it returns the actionable items as proposals. This return sits before
// the first agent() call; the dispatch below is unreachable on this path.
if (input.fixMode === 'propose') {
  const proposed = actionable.map((f) => ({
    path: f.path,
    applied: 0,
    skipped: 0,
    failed: 0,
    actions: [],
    proposed: f.remediations.filter((r) => r.decision !== 'skip'),
  }))
  const proposedCount = proposed.reduce((n, f) => n + f.proposed.length, 0)
  log(`Propose-only (audit.fix_mode = propose) -- no edits made; ${proposedCount} remediation(s) across ${proposed.length} skills reported as proposals`)
  return { perFile: proposed, summary: { applied: 0, skipped: 0, failed: 0, proposed: proposedCount }, fixMode: 'propose', routes: laneRoutes() }
}

function lanePrompt(f) {
  return `You are ONE lane of a SKILL.md remediation pass. Apply the decided edits for exactly one skill. Make ONLY the edits listed; do not audit, re-scan, or fix anything not listed here.

Target SKILL.md: ${f.path}

Remediations (apply in order):
${f.remediations.map((r, i) => `${i + 1}. [${r.bucket} / taxonomy ${r.taxonomy} / ${r.criterion}${r.line != null ? ` @ line ${r.line}` : ''}]
   instruction: ${r.instruction}
   decision: ${r.decision}`).join('\n')}

Rules:
- decision "apply"  -> make the edit exactly as the instruction describes.
- decision "skip"   -> do nothing for that item; record status "skipped".
- any other decision text -> treat it as a refined instruction and apply THAT instead of the original.
- Most edits touch the SKILL.md itself. Two taxonomies edit a sibling instead: taxonomy I (decision-provenance) MOVES the Dec-N lines from the SKILL.md into the skill's co-located CLAUDE.md (create it if absent), leaving only the resulting rule in SKILL.md; taxonomy H (ADP back-reference) edits the cited references/*.md to remove the back-citation. The instruction names the exact target file and lines.
- Use the Read tool to load the target file first, then Edit to make precise changes. Preserve surrounding formatting.
- If an edit cannot be applied safely (anchor not found, ambiguous), record status "failed" with a short note rather than guessing.

Return a summary: counts of applied/skipped/failed and a per-item action list.`
}

phase('Remediate')
// The model and effort come from args.laneModels through laneAgent (the lane
// route above); nothing is inherited from the session and nothing is pinned here.
const results = await laneParallel(actionable.map((f) => () =>
  laneAgent(f.path, lanePrompt(f), {
    label: `fix:${f.path.split(/[\\/]/).slice(-2).join('/')}`,
    phase: 'Remediate',
    schema: FILE_RESULT_SCHEMA,
    // Apply mode edits files; a codex entry gets a workspace-write sandbox.
    access: 'write',
  }).then((r) => (r ? { ...r, path: f.path } : null))
))

const summary = results.filter(Boolean).reduce(
  (acc, r) => {
    acc.applied += r.applied
    acc.skipped += r.skipped
    acc.failed += r.failed
    return acc
  },
  { applied: 0, skipped: 0, failed: 0 }
)
log(`Remediation across ${results.filter(Boolean).length} skills — applied ${summary.applied}, skipped ${summary.skipped}, failed ${summary.failed}`)

return { perFile: results.filter(Boolean), summary, routes: laneRoutes() }
