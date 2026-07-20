// skill-audit — DETECT workflow (before-Q&A phase).
//
// Fan-out detection + classification, one lane per target SKILL.md file. Each
// lane runs the mechanical validator (skills_kit_lib.audit) for the Schema
// group, applies the CCP / CRP / ADP / decision-provenance judgment from the
// recap embedded in the lane prompt, and classifies every finding into the
// A-L taxonomy + one of four dispositions (FIX / SERIOUS / IMPROVE / SILENT;
// K -> SPECIAL), assigned instance-level by the step-6 classifier. Report
// contract for the main loop: SERIOUS summarized at the top, FIX as an applied
// count (lands in the remediation CL), IMPROVE as a count + one-line pitches
// (opt-in), SILENT omitted entirely, no hedging. Pure detection — NO file is modified
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
          taxonomy: { type: 'string', description: 'taxonomy id A-L; "none" for PASS/INFO/JUDGMENT that need no remediation' },
          bucket: { type: 'string', enum: ['FIX', 'SERIOUS', 'IMPROVE', 'SILENT', 'SPECIAL', 'NONE'], description: 'per-finding disposition assigned instance-level by the classifier (step 6)' },
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
    ? `Run the mechanical validator via Bash (it is a package module, so cd into the plugin root first):\n    (cd "${refs.pluginRoot}" && "${refs.venvPython}" -m skills_kit_lib.audit "${f.path}" --json)\nMap its rows into Schema-group findings: a universal-rule or YAML-schema FAIL is a Schema FAIL. Specifically: missing/malformed required frontmatter -> taxonomy A (default FIX -- add the mechanical default; authorial fields route to IMPROVE); description length/directive-form/exclusion-clause FAIL -> taxonomy B (IMPROVE, authorial); a YAML contract FAIL (missing required key, wrong type, list below min_len, forbidden key) -> taxonomy E (default FIX for a missing-default field; authorial or forbidden-key -> IMPROVE); a mixed-type signal (>1 canonical root, or the mixed-type heuristic) -> taxonomy D (IMPROVE, unless the orientation-summary exception applies, then JUDGMENT); a load-graph row (orphaned references/ file, unlinked member directory, two-hop-only reference, dangling index entry) -> taxonomy L (IMPROVE default; a dangling index path with an identified correct target is a mechanical FIX; an accepted internal-helper orphan is SILENT), group ADP, keeping the validator's severity (FAIL gates; JUDGMENT does not). Assign the final disposition in step 6, not here. If the validator is unavailable, emit one Schema finding severity JUDGMENT ("validator unavailable") and continue — never fail a file for that.`
    : `Validator path was not provided; emit one Schema finding severity JUDGMENT ("validator unavailable") and continue with cohesion judgment only.`

  return `You are ONE lane of a SKILL.md audit. Audit exactly one file and return structured findings. This is DETECTION ONLY — do not modify any file.

Target:    ${f.path}
SkillType: ${f.skillType || '(read from frontmatter)'}

Steps:
1. Read the target SKILL.md. Note its frontmatter name + skill-type. Count lines and estimate tokens (~chars/4).
2. ${schemaClause}
3. Apply the cohesion-principle judgment for SKILL.md (this recap is self-contained; do NOT load any framework doc):
   - CCP (ccp_placement): SKILL.md content belongs here only when it changes WITH the skill's contract. Project-convention content (local code-review rules, project tool prefs — content that changes with project conventions) is misallocated; its home is the co-located CLAUDE.md. A violation is taxonomy F (DISCUSS), group CCP, severity JUDGMENT.
   - decision_provenance: Dec-N entries, "audit-finding" tags, dated decision-log lines change with audits, not the contract. In a SKILL.md body they are a FAIL — taxonomy I (FIX -- mechanical move to the co-located CLAUDE.md), group CCP. Detect Dec-\\d patterns / "audit-finding" / "decision log" markers.
   - CRP (crp_placement): SKILL.md is read together; references/ are loaded on-demand for DISTINCT sub-tasks. Body length over ~500 lines / ~3000 tokens is a SIGNAL to evaluate a split, never a verdict by itself. Only when sections genuinely serve different reading tasks AND the body is over threshold is it taxonomy G (DISCUSS), group CRP, JUDGMENT. A stub whose reference is always co-loaded is a tool-call doubling, not a win — do not propose that split.
   - ADP (adp_back_reference): reference docs under this skill's references/ must be one hop deep from SKILL.md and must NOT cite SKILL.md sections (a back-reference is a cycle). Read each references/*.md (if any) and check for back-citations to this SKILL.md. A back-reference is a FAIL — taxonomy H (FIX -- mechanical rewrite), group ADP.
   - Load-graph routing (references_reachable_from_skill_md, judgment half): the validator already surfaces missing edges mechanically; the lane adds only the keyword-adequacy call — for content a reference doc owns (its headings, entity names, script names), do the SKILL.md index entry's keywords carry the exact terms a searcher would use? A clear routing gap is taxonomy L (IMPROVE), group ADP, severity JUDGMENT.
4. Hygiene: body over ~500 lines or ~3000 tokens -> one INFO finding, group Hygiene, taxonomy J — a CRP-evaluation prompt, never a FAIL on its own; disposition IMPROVE when a concrete extraction candidate can be named, else SILENT.
5. Wrong-type signal (taxonomy C): only raise if the validator's type-specific rows or the body shape clearly contradict the declared skill-type. Emit as group Schema, severity JUDGMENT, disposition IMPROVE, and note that classify.py confirmation is deferred to the Q&A gate (the lane does NOT run classify.py).
6. DISPOSITION CLASSIFIER. Assign EVERY non-PASS finding a taxonomy id (A-L) and one of four dispositions -- FIX / SERIOUS / IMPROVE / SILENT (K -> SPECIAL). The taxonomy default bucket is a starting point only; decide instance-level against these predicates.

   Classifier prod (read this FIRST -- it overrides your default caution): You are biased toward conservatism; the user's time and attention are the scarce resources; source control and CL review are the safety net. If the edit very likely improves the doc, apply it.

   Master razor: FIX = anything decidable by VERIFIED FACTS plus DOCUMENTED PROJECT CONVENTIONS. Reserve IMPROVE for where no fact and no convention decides. The bar for FIX is: would a reasonable owner, seeing this diff in CL review, accept it without discussion? "Very likely improving" clears it.

   FIX (auto-applied; lands in a reviewable CL): a correction against a verified fact (a broken link/path with an identified target; a count/list/attribution/signature; a semantic claim corrected from a verified code reading); deletion of FALSIFIED content; a convention-violation fix (non-ASCII look-alike, hardcoded absolute/foreign-machine path, drifted line number, dedup under the summarize-and-reference rule -- REMINDER PLUS REFERENCE, a dozen tokens or less, else reference-only); a trim of a default / obvious-to-any-agent content (be aggressive). Mechanical skill fixes: A missing-default frontmatter, H back-reference rewrite, I Dec-N move. Loss-free-deletion guard ALWAYS before removing a duplicate/section: fold any local delta into the SSOT or summary line FIRST.
   SERIOUS (surface at the TOP, summarized, NEVER auto-fixed, never buried): a secret / security finding; a protective rail whose documented mechanism is fictional (the real finding is the unprotected invariant); a doc problem that reveals a real-world problem.
   IMPROVE (count + one-liners; opt-in): a structural move (B description rewrite, C type change, D split, F CCP move, G CRP split, L edge/keyword add; graduate/fold/absorb/orphan-link); or a trim of TRUE content passing the one-line test. E authorial-schema-choice is IMPROVE.
   SILENT (do NOT surface; no hedging): a do-nothing conclusion; a validator detection artifact; an accepted structural pattern (an agent-definition file with zero inbound citations, a historical record, a companion-source PDF, an internal-helper orphan).
   SPECIAL = K only (escape hatch).

   Ambiguity rulings (apply when the disposition is unclear):
   1. Your own verified code-reading DISCHARGES any "confirm with author" hedge -- if the lane verified the actual behavior from code, the correction is FIX; do not leak it back into discussion.
   2. A generator-owned path absent from the checkout (materializes on doc-gen, like Generated/) is NOT a broken link. Adding the annotation "auto-generated (present after doc-gen)" is FIX; repointing or deleting stays IMPROVE.
   3. When a duplicate is both auto-dedupable and part of a larger opt-in relocation: FIX the dedup now (collapse to summary + reference); the relocation stays a separate IMPROVE. Dedup never waits on structure.
   4. A CRP/size split is offerable (IMPROVE) only when you can NAME a concrete extraction candidate; a bare over-threshold nudge with no named candidate is SILENT (mirror of the one-line trim test).
   5. A validator detection artifact is SILENT only when placating the validator needs no real doc change. If the same edit is ALSO a genuine project-convention fix (e.g. backslash paths -> forward slashes), it is FIX.

   Declined-opportunity ledger: if the SKILL.md frontmatter carries an \`md-audit-declined:\` list (bare taxonomy ids or short finding keys), do NOT re-raise an IMPROVE finding the user already declined for that file -- honor it exactly like references-audit honors \`references-audit-allow-stale\`. A new or materially different finding still fires.
   PASS / INFO / JUDGMENT findings that need no remediation get taxonomy "none" and bucket "NONE".
   For each FIX/SERIOUS/IMPROVE/SPECIAL finding write a concrete \`remediation\` (FIX = the edit it will apply; SERIOUS = the one-line top-of-report summary; IMPROVE = the single one-line pitch), with line refs.
7. Verdict: NON-COMPLIANT if ANY finding has severity FAIL; otherwise COMPLIANT. INFO/JUDGMENT never gate. Disposition is orthogonal to the verdict.

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
    if (fnd.bucket === 'FIX') acc.fix++
    else if (fnd.bucket === 'SERIOUS') acc.serious++
    else if (fnd.bucket === 'IMPROVE') acc.improve++
    else if (fnd.bucket === 'SILENT') acc.silent++
    else if (fnd.bucket === 'SPECIAL') acc.special++
    if (fnd.severity === 'FAIL') acc.fail++
  }
  if (r.verdict === 'NON-COMPLIANT') acc.nonCompliant++
  return acc
}, { fix: 0, serious: 0, improve: 0, silent: 0, special: 0, fail: 0, nonCompliant: 0 })

log(`Audited ${results.length}/${input.files.length} SKILL.md files — ${totals.nonCompliant} NON-COMPLIANT, ${totals.fail} FAIL findings; dispositions SERIOUS=${totals.serious} FIX=${totals.fix} IMPROVE=${totals.improve} (SILENT=${totals.silent} omitted)`)

return { perFile: results, totals }
