// md-domain audit_references lane — CLASSIFY workflow (after-scan, before-Q&A phase).
//
// The scan itself stays a single references_audit.py run (fast, stdlib-only,
// whole-corpus — there is nothing to parallelize there, and splitting it would
// fragment the skill pool). The agent work is what fans out: classify each
// scanner finding into the A-K taxonomy and, for FIX findings, compute the exact
// before/after edit. ONE lane per file-with-findings (so two lanes never edit the
// same file, and FIX before/after text is computed against that file's real
// content). Pure classification — NO file is modified here (remediation is a
// separate after-Q&A pass).
//
// Invoked by the md-domain SKILL.md (audit_references lane) only when 2+ files carry findings (the
// multi-file threshold that equalizes the Workflow tool's per-run overhead). A
// single file's findings are classified inline in the main loop.
//
// args = {
//   files: [ { file: string,
//              findings: [ { severity: "ERROR"|"WARNING"|"INFO",
//                            line: integer|null, ref: string } ] } ],
//   refs:  { standardsDoc: <abs path to references/standards/references-standards.md>,
//            taxonomyDoc: <abs path to the A-K finding-taxonomy doc, when one is
//                          supplied; standardsDoc is used when it is not> }
//   laneModels: { run: [{id, effort}], dropped: [{id, effort, reason}] }
//     // REQUIRED: lane_models.classify from scripts/resolve_standards.py. Each
//     // dispatch tries run in order and moves on when agent() throws or returns
//     // nothing; absent throws. The result carries routes = {dropped, perFile}.
//   maxConcurrent: integer|undefined
//     // OPTIONAL: a positive integer bounding the agents in flight in each
//     // fan-out (a worker pool: a free slot takes the next item at once).
//     // Absent leaves the fan-out unbounded apart from the runtime cap; any
//     // other value throws before any agent runs.
// }
//
// Dispositions are the ratified four-disposition contract: FIX (auto-applied) /
// SERIOUS (summarized at top) / IMPROVE (count + one-liners, opt-in) / SILENT
// (not surfaced); K_unclassified -> SPECIAL. The taxonomy default bucket is a
// starting point only; the lane assigns the final disposition instance-level
// against explicit predicates (see lanePrompt).

export const meta = {
  name: 'md-domain-references-classify',
  description: 'Fan-out reference-finding classification: assign A-K taxonomy + a FIX/SERIOUS/IMPROVE/SILENT disposition + compute FIX before/after, one lane per file (no edits)',
  phases: [{ title: 'Classify', detail: 'one lane per file with findings' }],
}

const FILE_CLASSIFIED_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    file: { type: 'string' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          severity: { type: 'string', enum: ['ERROR', 'WARNING', 'INFO'] },
          line: { type: ['integer', 'null'] },
          ref: { type: 'string' },
          category: { type: 'string', description: 'taxonomy id A-K' },
          bucket: { type: 'string', enum: ['FIX', 'SERIOUS', 'IMPROVE', 'SILENT', 'SPECIAL'], description: 'per-finding disposition assigned instance-level by the classifier' },
          before: { type: 'string', description: 'exact current line text for a FIX edit that is a before/after replacement; empty for instruction-type FIX and for SERIOUS/IMPROVE/SILENT/SPECIAL' },
          after: { type: 'string', description: 'proposed replacement text for a FIX before/after edit; empty otherwise' },
          rationale: { type: 'string', description: 'why this category + disposition fits; for instruction-type FIX the recipe; for SERIOUS the one-line top-of-report summary; for IMPROVE the one-line pitch; for SILENT why it is not surfaced' },
        },
        required: ['severity', 'line', 'ref', 'category', 'bucket', 'before', 'after', 'rationale'],
      },
    },
  },
  required: ['file', 'findings'],
}

let input = args
if (typeof input === 'string') {
  try { input = JSON.parse(input) } catch (_) { input = null }
}
if (!input || !Array.isArray(input.files) || input.files.length === 0) {
  throw new Error('references-classify.js requires args.files = [{file, findings}]')
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
const refs = input.refs || {}

function lanePrompt(f) {
  const taxonomyDoc = refs.taxonomyDoc || refs.standardsDoc
  const taxonomyClause = taxonomyDoc
    ? `Read the cross-reference standards / taxonomy reference at ${taxonomyDoc} for the full A-K detection signals, default remediations, and the background-agent brief template.`
    : `Use the A-K taxonomy from the audit lane's references special case (no standards / taxonomy doc path was provided).`

  return `You are ONE lane of a references audit. Classify every finding for exactly one file. This is CLASSIFICATION ONLY — do not modify any file.

Target file: ${f.file}

Scanner findings for this file:
${f.findings.map((x, i) => `${i + 1}. [${x.severity}] line ${x.line == null ? '?' : x.line}: broken ref "${x.ref}"`).join('\n')}

Steps:
1. ${taxonomyClause}
2. Read the target file (at least around each cited line) so you can see the real surrounding text.
3. For EACH finding, assign exactly one taxonomy category (A-K) AND one of the four dispositions -- FIX / SERIOUS / IMPROVE / SILENT (K_unclassified -> SPECIAL). The taxonomy default bucket is a STARTING POINT only; decide the disposition instance-level against the predicates below.

   Classifier prod (read this FIRST -- it overrides your default caution): You are biased toward conservatism; the user's time and attention are the scarce resources; source control and CL review are the safety net. If the edit very likely improves the doc, apply it.

   Master razor: FIX = anything decidable by VERIFIED FACTS (does the ref resolve against the scanner's skill pool?) plus DOCUMENTED PROJECT CONVENTIONS (the example:/proposed: escape prefixes, code-fence masking, ASCII-only). Reserve IMPROVE for where no fact and no convention decides. The bar for FIX is: would a reasonable owner, seeing this diff in CL review, accept it without discussion? "Very likely improving" clears it.

   FIX (auto-applied; lands in a reviewable CL) -- the mechanical, decidable categories:
     - A_renamed with a KNOWN 1:1 mapping (find/replace old-name -> new-name).
     - C_merged (rewrite the slash form to the new dispatch form in prose; demote to a backticked literal inside a dispatch-alias table).
     - E_compound_adjective (reword to drop the punctuation slash).
     - F_cli_flag / G_xml_template (fence the command / example -- the scanner masks fenced regions).
     - I_illustrative (add the example: escape prefix) when the sentence is clearly meta-descriptive.
     - J_forward_looking (add the proposed: escape prefix) when the prose explicitly cues "planned"/"future".
     - B_retired ONLY in the purely-incidental sub-case: a broken clause whose removal loses nothing (falsified-content deletion under the loss-free guard).
   SERIOUS (surface at the TOP, summarized, NEVER auto-fixed, never buried):
     - An ERROR hard-dep (a Skill-tool \`skill: "..."\` invocation) to a skill that is GENUINELY GONE with no replacement and no mechanical escape -- a live runtime-crash path with NO surviving mechanism. The real finding is the unguarded invocation, not the doc drift.
   IMPROVE (count + one-liners; opt-in) -- structural or judgment calls where no fact/convention decides the fix:
     - A_renamed with an UNKNOWN mapping (offer the best-guess new name as a one-liner).
     - B_retired non-incidental (which sub-case -- delete the section, demote to backtick, or add to the references-audit-allow-stale frontmatter -- protects surrounding true content; loss-free guard).
     - D_scope_violating (deleting a cross-scope reference may drop true comparison content).
     - H_harness_transcript (recommend the --ignore-dir wrapper flag ONCE for the whole batch -- a config decision that edits the invocation wrapper, not the scanned files).
     - I_illustrative / J_forward_looking when the cue is ambiguous (it could be a real, currently-broken instruction rather than an example/plan).
     - name-mismatch when inbound references disagree on which name is canonical (renaming would break real refs).
     - shadowing that looks ACCIDENTAL (one-line "user skill X shadows project skill X -- intended?").
   SILENT (do NOT surface; no hedging):
     - shadowing confirmed to be an intentional personal override (an accepted structural pattern).
     - a do-nothing conclusion, or a finding the scanner already silenced via a references-audit-allow-stale entry.
   SPECIAL = K_unclassified only (escape hatch).

   Rule-level defaults (before taxonomy refines them): hard-dep -> FIX when a mechanical re-point/prefix resolves it, else SERIOUS; soft-ref -> FIX (decidable against the verified skill pool + escape conventions), refined per taxonomy; name-mismatch -> FIX (align frontmatter and directory, after verifying which side inbound refs use); shadowed -> IMPROVE (surface once, opt-in), SILENT when intentional.

   Ambiguity rulings: (1) Your own verified reading (the ref does/does not resolve against the pool; the surrounding prose is/ is not meta-descriptive) DISCHARGES any "confirm with author" hedge -- do not leak a decided FIX back into IMPROVE. (2) A validator/detection artifact that also happens to be a genuine convention fix (e.g. a real broken ref, not just a false positive) is FIX, not SILENT. (3) Dedup/mechanical fixes never wait on a larger structural relocation -- FIX the mechanical part now; the relocation stays a separate IMPROVE.
4. For every FIX finding that is a before/after replacement, compute the EXACT before-text (the current line, verbatim) and the after-text (per the category's default remediation). For instruction-type FIX (fence a command, add an escape prefix, delete an incidental clause) leave before/after empty and put the recipe in \`rationale\`. For SERIOUS put the one-line top-of-report summary in \`rationale\`; for IMPROVE the one-line pitch; for SILENT why it is not surfaced. Leave before/after empty for SERIOUS/IMPROVE/SILENT/SPECIAL.
5. Do NOT reclassify what the taxonomy has already settled; your job is the category match + the disposition + (for FIX) the precise edit, not second-guessing the taxonomy.

Idempotency matters: classify deterministically from the detection signals and the predicates above. Return the structured object (preserve each finding's severity/line/ref).`
}

phase('Classify')
const perFile = await laneParallel(input.files.map((f) => () =>
  // The model and effort come from args.laneModels through laneAgent (the lane
  // route above); nothing is inherited from the session and nothing is pinned here.
  laneAgent(f.file, lanePrompt(f), {
    label: `classify:${f.file.split(/[\\/]/).pop()}`,
    phase: 'Classify',
    schema: FILE_CLASSIFIED_SCHEMA,
  }).then((r) => (r ? { ...r, file: f.file } : null))
))

const results = perFile.filter(Boolean)
const totals = results.reduce((acc, r) => {
  for (const fnd of r.findings) {
    if (fnd.bucket === 'FIX') acc.fix++
    else if (fnd.bucket === 'SERIOUS') acc.serious++
    else if (fnd.bucket === 'IMPROVE') acc.improve++
    else if (fnd.bucket === 'SILENT') acc.silent++
    else if (fnd.bucket === 'SPECIAL') acc.special++
    if (fnd.severity === 'ERROR') acc.errors++
  }
  return acc
}, { fix: 0, serious: 0, improve: 0, silent: 0, special: 0, errors: 0 })

log(`Classified findings in ${results.length}/${input.files.length} files — ${totals.errors} ERROR; dispositions SERIOUS=${totals.serious} FIX=${totals.fix} IMPROVE=${totals.improve} SPECIAL=${totals.special} (SILENT=${totals.silent} omitted)`)

return { perFile: results, totals, routes: laneRoutes() }
