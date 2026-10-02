// md-domain audit_references lane — REMEDIATE workflow (after-Q&A phase).
//
// Fan-out remediation, one lane per file, applying the decided reference fixes
// the main loop gathered during the Q&A gate (interactive IMPROVE/SPECIAL) or the
// FIX bucket (auto-applied, no decision needed). Runs AFTER classification + the
// user decision step — never folded into classification (detection and
// remediation stay separate so re-running the scan reproduces the same findings).
//
// One lane per FILE (not per finding) so two lanes never edit the same file
// concurrently; within a lane, edits are applied in order. No worktree isolation:
// lanes touch disjoint files, so they cannot conflict.
//
// Invoked by the md-domain SKILL.md (audit_references lane) only when remediation work spans 2+
// files (the multi-file threshold that equalizes Workflow-tool overhead). A
// single file's edits are applied inline in the main loop.
//
// args = {
//   perFile: [ {
//     file: string,
//     edits: [ {
//       category: string, bucket: "FIX"|"SERIOUS"|"IMPROVE"|"SILENT"|"SPECIAL",
//       line: integer|null,
//       before: string,                // exact current text (before/after FIX)
//       after: string,                 // replacement text (before/after FIX)
//       instruction: string,           // human-readable edit description (instruction-type FIX / IMPROVE / SPECIAL)
//       decision: "apply"|"skip"|string  // user/inferred decision; free-text = refined instruction
//     } ]
//   } ],
//   fixMode: "apply"|"propose",  // REQUIRED: audit.fix_mode from
//                                // scripts/resolve_standards.py; absent throws
//   laneModels: { run: [{id, effort}], dropped: [{id, effort, reason}] }
//                               // REQUIRED: lane_models.remediate from
//                               // scripts/resolve_standards.py; absent throws
// }

export const meta = {
  name: 'md-domain-references-remediate',
  description: 'Fan-out reference-fix remediation: apply the decided edits, one lane per file (after-Q&A phase)',
  phases: [{ title: 'Remediate', detail: 'one lane per file' }],
}

const FILE_RESULT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    file: { type: 'string' },
    applied: { type: 'integer' },
    skipped: { type: 'integer' },
    failed: { type: 'integer' },
    actions: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          category: { type: 'string' },
          status: { type: 'string', enum: ['applied', 'skipped', 'failed'] },
          note: { type: 'string' },
        },
        required: ['category', 'status', 'note'],
      },
    },
  },
  required: ['file', 'applied', 'skipped', 'failed', 'actions'],
}

let input = args
if (typeof input === 'string') {
  try { input = JSON.parse(input) } catch (_) { input = null }
}
if (!input || !Array.isArray(input.perFile) || input.perFile.length === 0) {
  throw new Error('remediate.js requires args.perFile = [{file, edits}]')
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
// run is the ordered list of {id, effort} entries agent() can run; dropped lists
// the declared entries it cannot run, each with its reason. An absent or
// malformed value throws before any agent is dispatched: reading "not passed" as
// "inherit the session model" is the default this argument exists to remove.
const LANE_CORE_IDS = ['fable', 'haiku', 'opus', 'sonnet']
const laneModels = input.laneModels
const isLaneEntry = (e) => !!e && typeof e === 'object' &&
  typeof e.id === 'string' && e.id.trim() !== '' &&
  typeof e.effort === 'string' && e.effort.trim() !== ''
if (!laneModels || typeof laneModels !== 'object' || !Array.isArray(laneModels.run) ||
    laneModels.run.length === 0 || !laneModels.run.every(isLaneEntry) || !Array.isArray(laneModels.dropped)) {
  throw new Error(`md-domain lane requires args.laneModels = {run: [{id, effort}, ...], dropped: [...]} with a non-empty run list, got ${JSON.stringify(laneModels) ?? 'nothing'}. Pass this lane family's lane_models entry from scripts/resolve_standards.py; an absent route is never read as "inherit the session model".`)
}
const laneUnrunnable = laneModels.run.filter((e) => !LANE_CORE_IDS.includes(e.id))
if (laneUnrunnable.length > 0) {
  throw new Error(`md-domain lane: args.laneModels.run names ${laneUnrunnable.map((e) => JSON.stringify(e.id)).join(', ')}, which agent() cannot run (runnable ids: ${LANE_CORE_IDS.join(', ')}). The resolver moves such ids to laneModels.dropped; a run list that still carries one was not produced by it.`)
}
const laneEntryText = (e) => String(e.id) + ' (' + String(e.effort) + ')'
if (laneModels.dropped.length > 0) {
  log('md-domain lanes: dropped ' + laneModels.dropped.map(laneEntryText).join(', ') +
    ' -- not runnable on agent(); running ' + laneModels.run.map(laneEntryText).join(', '))
}
const laneRoutePerFile = []
// Dispatch one lane through the run list. Entries are tried in order; the next
// one runs when agent() throws or returns nothing. A lane whose every entry
// failed returns null, as a single agent() call that died does, and each
// failure stays on its route record.
async function laneAgent(key, prompt, opts) {
  const failed = []
  for (const e of laneModels.run) {
    let r = null
    try {
      r = await agent(prompt, { ...opts, model: e.id, effort: e.effort })
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
// lane-route: end

// Drop files whose every edit is a skip — nothing to do, no lane needed.
const actionable = input.perFile.filter(
  (f) => Array.isArray(f.edits) && f.edits.some((e) => e.decision !== 'skip')
)

// Propose-only guard. args.fixMode comes from resolve_standards.py's
// `audit.fix_mode`. When it is "propose" the lane makes NO edit and dispatches NO
// agent: it returns the actionable items as proposals. This return sits before
// the first agent() call; the dispatch below is unreachable on this path.
if (input.fixMode === 'propose') {
  const proposed = actionable.map((f) => ({
    file: f.file,
    applied: 0,
    skipped: 0,
    failed: 0,
    actions: [],
    proposed: f.edits.filter((e) => e.decision !== 'skip'),
  }))
  const proposedCount = proposed.reduce((n, f) => n + f.proposed.length, 0)
  log(`Propose-only (audit.fix_mode = propose) -- no edits made; ${proposedCount} edit(s) across ${proposed.length} files reported as proposals`)
  return { perFile: proposed, summary: { applied: 0, skipped: 0, failed: 0, proposed: proposedCount }, fixMode: 'propose', routes: laneRoutes() }
}

function lanePrompt(f) {
  return `You are ONE lane of a references remediation pass. Apply the decided reference fixes to exactly one file. Make ONLY the edits listed; do not re-scan or fix anything not listed here.

Target file: ${f.file}

Edits (apply in order):
${f.edits.map((e, i) => `${i + 1}. [${e.bucket} / category ${e.category}${e.line != null ? ` @ line ${e.line}` : ''}]
   ${e.before ? `before: ${JSON.stringify(e.before)}\n   after:  ${JSON.stringify(e.after)}` : `instruction: ${e.instruction}`}
   decision: ${e.decision}`).join('\n')}

Rules:
- decision "apply" + before/after present -> replace the exact before-text with the after-text at the cited line.
- decision "apply" + instruction only -> perform the described edit (e.g. wrap a command in a fenced code block, add a per-file allow-stale frontmatter entry, delete a section).
- decision "skip" -> do nothing for that item; record status "skipped".
- any other decision text -> treat it as a refined instruction and apply THAT instead.
- Use the Read tool to load the file first, then Edit to make precise changes. Preserve surrounding formatting.
- If the before-text no longer matches (the file changed) or the edit is ambiguous, record status "failed" with a short note rather than guessing.

Return a summary: counts of applied/skipped/failed and a per-item action list.`
}

phase('Remediate')
// The model and effort come from args.laneModels through laneAgent (the lane
// route above); nothing is inherited from the session and nothing is pinned here.
const results = await parallel(actionable.map((f) => () =>
  laneAgent(f.file, lanePrompt(f), {
    label: `fix:${f.file.split(/[\\/]/).pop()}`,
    phase: 'Remediate',
    schema: FILE_RESULT_SCHEMA,
  }).then((r) => (r ? { ...r, file: f.file } : null))
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
log(`Remediation across ${results.filter(Boolean).length} files — applied ${summary.applied}, skipped ${summary.skipped}, failed ${summary.failed}`)

return { perFile: results.filter(Boolean), summary, routes: laneRoutes() }
