// skill-audit — DETECT workflow (before-Q&A phase).
//
// Fan-out detection + classification, one lane per target SKILL.md file. Each
// lane runs the mechanical validator (skills_kit_lib.audit) for the Schema
// group, applies the CCP / CRP / ADP / decision-provenance judgment from the
// recap embedded in the lane prompt, and classifies every finding into the
// A-K taxonomy + a remediation bucket. Pure detection — NO file is modified
// here (the skill's `audit_then_self_remediate` anti-pattern keeps detection and
// remediation in separate phases). Returns structured per-file findings for the
// main loop to render and dispatch.
//
// Cache efficiency: each fan-out lane is an isolated context whose prompt prefix
// is NOT shared across siblings, so the lane carries the compact skill-md
// criteria recap inline rather than loading cohesion-principles per lane (the
// upstream framework is the derivation, not the operative rules).
//
// Invoked by the skill-audit SKILL.md only when auditing 2+ files (the multi-file
// threshold that equalizes the Workflow tool's per-run overhead). Single-file
// audits run inline in the main loop.
//
// args = {
//   files: [ { path: string, skillType?: string } ],
//   refs:  { pluginRoot: <abs path to plugins/skills-kit (parent of skills_kit_lib)>,
//            venvPython: <abs path to skills-kit venv python> }
// }
// The mechanical validator is invoked as a module:
//   (cd <pluginRoot> && <venvPython> -m skills_kit_lib.audit <file> --json)

export const meta = {
  name: 'skill-audit-detect',
  description: 'Fan-out SKILL.md audit: validate contract + apply CCP/CRP/ADP + decision-provenance + classify, one lane per file (detection only, no edits)',
  phases: [{ title: 'Audit', detail: 'one lane per SKILL.md file' }],
}

const FILE_FINDINGS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    path: { type: 'string' },
    skill_name: { type: 'string' },
    skill_type: { type: 'string' },
    lines: { type: 'integer' },
    approx_tokens: { type: 'integer' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          group: { type: 'string', enum: ['Schema', 'CCP', 'CRP', 'ADP', 'Hygiene'] },
          severity: { type: 'string', enum: ['PASS', 'FAIL', 'INFO', 'JUDGMENT'] },
          criterion: { type: 'string', description: 'criterion id or short name, e.g. ccp_placement' },
          message: { type: 'string' },
          line: { type: ['integer', 'null'], description: 'line number in the file, or null' },
          taxonomy: { type: 'string', description: 'taxonomy id A-K; "none" for PASS/INFO/JUDGMENT that need no remediation' },
          bucket: { type: 'string', enum: ['AUTO', 'DISCUSS', 'SPECIAL', 'NONE'] },
          remediation: { type: 'string', description: 'concrete proposed remediation for AUTO/DISCUSS/SPECIAL; empty for NONE' },
        },
        required: ['group', 'severity', 'criterion', 'message', 'line', 'taxonomy', 'bucket', 'remediation'],
      },
    },
    verdict: { type: 'string', enum: ['COMPLIANT', 'NON-COMPLIANT'] },
  },
  required: ['path', 'skill_name', 'skill_type', 'lines', 'approx_tokens', 'findings', 'verdict'],
}

// args may arrive as an object or as a JSON string depending on how the
// invoker passes it; normalize to an object.
let input = args
if (typeof input === 'string') {
  try { input = JSON.parse(input) } catch (_) { input = null }
}
if (!input || !Array.isArray(input.files) || input.files.length === 0) {
  throw new Error('detect.js requires args.files = [{path}]')
}
const refs = input.refs || {}

function lanePrompt(f) {
  const schemaClause = refs.pluginRoot && refs.venvPython
    ? `Run the mechanical validator via Bash (it is a package module, so cd into the plugin root first):\n    (cd "${refs.pluginRoot}" && "${refs.venvPython}" -m skills_kit_lib.audit "${f.path}" --json)\nMap its rows into Schema-group findings: a universal-rule or YAML-schema FAIL is a Schema FAIL. Specifically: missing/malformed required frontmatter -> taxonomy A (AUTO, mechanical default); description length/directive-form/exclusion-clause FAIL -> taxonomy B (DISCUSS); a YAML contract FAIL (missing required key, wrong type, list below min_len, forbidden key) -> taxonomy E (DISCUSS); a mixed-type signal (>1 canonical root, or the mixed-type heuristic) -> taxonomy D (DISCUSS, unless the orientation-summary exception applies, then JUDGMENT). If the validator is unavailable, emit one Schema finding severity JUDGMENT ("validator unavailable") and continue — never fail a file for that.`
    : `Validator path was not provided; emit one Schema finding severity JUDGMENT ("validator unavailable") and continue with cohesion judgment only.`

  return `You are ONE lane of a SKILL.md audit. Audit exactly one file and return structured findings. This is DETECTION ONLY — do not modify any file.

Target:    ${f.path}
SkillType: ${f.skillType || '(read from frontmatter)'}

Steps:
1. Read the target SKILL.md. Note its frontmatter name + skill-type. Count lines and estimate tokens (~chars/4).
2. ${schemaClause}
3. Apply the cohesion-principle judgment for SKILL.md (this recap is self-contained; do NOT load any framework doc):
   - CCP (ccp_placement): SKILL.md content belongs here only when it changes WITH the skill's contract. Project-convention content (local code-review rules, project tool prefs — content that changes with project conventions) is misallocated; its home is the co-located CLAUDE.md. A violation is taxonomy F (DISCUSS), group CCP, severity JUDGMENT.
   - decision_provenance: Dec-N entries, "audit-finding" tags, dated decision-log lines change with audits, not the contract. In a SKILL.md body they are a FAIL — taxonomy I (AUTO), group CCP. Detect Dec-\\d patterns / "audit-finding" / "decision log" markers.
   - CRP (crp_placement): SKILL.md is read together; references/ are loaded on-demand for DISTINCT sub-tasks. Body length over ~500 lines / ~3000 tokens is a SIGNAL to evaluate a split, never a verdict by itself. Only when sections genuinely serve different reading tasks AND the body is over threshold is it taxonomy G (DISCUSS), group CRP, JUDGMENT. A stub whose reference is always co-loaded is a tool-call doubling, not a win — do not propose that split.
   - ADP (adp_back_reference): reference docs under this skill's references/ must be one hop deep from SKILL.md and must NOT cite SKILL.md sections (a back-reference is a cycle). Read each references/*.md (if any) and check for back-citations to this SKILL.md. A back-reference is a FAIL — taxonomy H (AUTO), group ADP.
4. Hygiene: body over ~500 lines or ~3000 tokens -> one INFO finding, group Hygiene, taxonomy J (DISCUSS) — a CRP-evaluation prompt, never a FAIL on its own.
5. Wrong-type signal (taxonomy C): only raise if the validator's type-specific rows or the body shape clearly contradict the declared skill-type. Emit as group Schema, severity JUDGMENT, bucket DISCUSS, and note that classify.py confirmation is deferred to the Q&A gate (the lane does NOT run classify.py).
6. Classify EVERY non-PASS finding into a taxonomy id (A-K) and bucket:
     - AUTO    = mechanical, safe to auto-apply (A: add missing field with a sensible default; H: rewrite back-citing sentence; I: move Dec-N to co-located CLAUDE.md)
     - DISCUSS = needs a user decision (B, C, D, E, F, G, J)
     - SPECIAL = K, unclassified
   PASS / INFO / JUDGMENT findings that need no remediation get taxonomy "none" and bucket "NONE".
   For each AUTO/DISCUSS/SPECIAL finding write a concrete \`remediation\` (what edit you propose, with line refs).
7. Verdict: NON-COMPLIANT if ANY finding has severity FAIL; otherwise COMPLIANT. INFO/JUDGMENT never gate.

Idempotency matters: apply the fixed criteria and taxonomy deterministically. Do not invent findings; report only what the criteria actually surface. Return the structured object.`
}

phase('Audit')
const perFile = await parallel(input.files.map((f) => () =>
  // Default lane tier: opus at high effort. Detection is the audits' judgment
  // core — criteria application warrants the judge tier, explicitly pinned.
  agent(lanePrompt(f), {
    label: `audit:${f.path.split(/[\\/]/).pop() === 'SKILL.md' ? f.path.split(/[\\/]/).slice(-2).join('/') : f.path.split(/[\\/]/).pop()}`,
    phase: 'Audit',
    model: 'opus',
    effort: 'high',
    schema: FILE_FINDINGS_SCHEMA,
  }).then((r) => ({ ...r, path: f.path }))
))

const results = perFile.filter(Boolean)
const totals = results.reduce((acc, r) => {
  for (const fnd of r.findings) {
    if (fnd.bucket === 'AUTO') acc.auto++
    else if (fnd.bucket === 'DISCUSS') acc.discuss++
    else if (fnd.bucket === 'SPECIAL') acc.special++
    if (fnd.severity === 'FAIL') acc.fail++
  }
  if (r.verdict === 'NON-COMPLIANT') acc.nonCompliant++
  return acc
}, { auto: 0, discuss: 0, special: 0, fail: 0, nonCompliant: 0 })

log(`Audited ${results.length}/${input.files.length} SKILL.md files — ${totals.nonCompliant} NON-COMPLIANT, ${totals.fail} FAIL findings; buckets AUTO=${totals.auto} DISCUSS=${totals.discuss} SPECIAL=${totals.special}`)

return { perFile: results, totals }
