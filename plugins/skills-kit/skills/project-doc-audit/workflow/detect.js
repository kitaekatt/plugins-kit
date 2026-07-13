// project-doc-audit — DETECT workflow (before-Q&A phase).
//
// Fan-out detection + classification, one lane per target project document. Each
// lane reads the doc, loads the SINGLE self-contained audit-criteria doc, applies
// the cohesion-principles project_reference_md role criteria using the mechanical
// signals discover.py already computed (orphan = inbound_citations, size =
// lines/approx_tokens), and classifies every finding into the taxonomy + a
// remediation bucket. Cache efficiency: each fan-out lane is an isolated context
// whose prompt prefix is NOT shared across siblings, so the lane loads exactly
// ONE criteria doc — the upstream cohesion-principles framework is the derivation,
// not the operative rules, and is intentionally not read here. Pure detection —
// NO file is modified here (the skill's `audit_then_self_remediate` anti-pattern
// keeps detection and remediation in separate phases). Returns structured per-file
// findings for the main loop to render and dispatch.
//
// Invoked by the project-doc-audit SKILL.md only when auditing 2+ files (the
// multi-file threshold that equalizes the Workflow tool's per-run overhead).
// Single-file audits run inline in the main loop.
//
// args = {
//   files: [ { path: string, kind: string, lines: integer,
//              approx_tokens: integer, inbound_citations: integer,
//              cited_by: string[] } ],
//   refs:  { criteria: <abs path to references/audit-criteria.md>,
//            pluginRoot: <abs path to plugins/skills-kit> }
// }

export const meta = {
  name: 'project-doc-audit-detect',
  description: 'Fan-out project-document audit: read + apply the project_reference_md criteria (maturation/CRP/ADP/CCP) + classify, one lane per file (detection only, no edits)',
  phases: [{ title: 'Audit', detail: 'one lane per project document' }],
}

const FILE_FINDINGS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    path: { type: 'string' },
    kind: { type: 'string', enum: ['project_doc', 'skill_reference', 'other_claude_artifact'] },
    lines: { type: 'integer' },
    approx_tokens: { type: 'integer' },
    inbound_citations: { type: 'integer' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          group: { type: 'string', enum: ['Placement', 'CRP', 'ADP', 'CCP', 'Hygiene'] },
          severity: { type: 'string', enum: ['PASS', 'FAIL', 'INFO', 'JUDGMENT'] },
          criterion: { type: 'string', description: 'criterion id or short name, e.g. placement_maturation' },
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
  required: ['path', 'kind', 'lines', 'approx_tokens', 'inbound_citations', 'findings', 'verdict'],
}

// args may arrive as an object or as a JSON string depending on how the
// invoker passes it; normalize to an object.
let input = args
if (typeof input === 'string') {
  try { input = JSON.parse(input) } catch (_) { input = null }
}
if (!input || !Array.isArray(input.files) || input.files.length === 0) {
  throw new Error('detect.js requires args.files = [{path, kind, lines, approx_tokens, inbound_citations, cited_by}]')
}
const refs = input.refs || {}

function lanePrompt(f) {
  const routingClause = f.kind && f.kind !== 'project_doc'
    ? `NOTE: discover.py classified this target as \`${f.kind}\`, NOT a project document. Emit exactly ONE finding: group "Placement", criterion "placement_not_in_skill_dir", severity INFO, taxonomy "A_misclassified_skill_ref", bucket DISCUSS, message naming the correct auditor (skill_reference -> /md-audit skill via its owning SKILL.md; other_claude_artifact -> /md-audit skill or /md-audit claude-md). Verdict COMPLIANT. Do NOT apply the other criteria.`
    : `This is a genuine project document — apply all the criteria below.`

  const orphanClause = f.inbound_citations === 0
    ? `discover.py reports ZERO inbound citations — this doc is an ORPHAN in the agent load graph. Raise PD-4 (group ADP, criterion adp_discoverability, severity JUDGMENT, taxonomy H_orphan, bucket DISCUSS). Offer the three paths in the remediation: add a CLAUDE.md pointer (make agent-reachable) / retire (dead) / accept as intentionally human-only (PASS). Do NOT auto-FAIL — a doc can legitimately serve human readers who open it directly.`
    : `discover.py reports ${f.inbound_citations} inbound citation(s); the doc is reachable in the load graph. PD-4 PASSes (no orphan finding) unless you see a more specific discoverability problem.`

  const sizeClause = (f.lines > 500 || f.approx_tokens > 3000)
    ? `Size signal: ${f.lines} effective lines / ~${f.approx_tokens} tokens is OVER the threshold. Run the PD-3 unitary-reading-task evaluation: do the sections fire on genuinely different sub-triggers? If yes -> JUDGMENT E_crp_split (group CRP); if no -> INFO J_size_signal only (a large single-task doc is correct). Size alone is NEVER a FAIL.`
    : `Size (${f.lines} lines / ~${f.approx_tokens} tokens) is under the threshold; raise a CRP split finding only if the sections obviously serve different reading tasks regardless of size.`

  return `You are ONE lane of a project-document audit. Audit exactly one file and return structured findings. This is DETECTION ONLY — do not modify any file.

Target: ${f.path}
Kind:   ${f.kind || 'project_doc'}
Signals: ${f.lines} lines, ~${f.approx_tokens} tokens, ${f.inbound_citations} inbound citations${Array.isArray(f.cited_by) && f.cited_by.length ? ` (e.g. ${f.cited_by.slice(0, 3).join(', ')})` : ''}

${routingClause}

Steps:
1. Read the target file.
2. Read the audit criteria at ${refs.criteria}. This file is self-contained: every testable rule is stated with the cohesion principle it derives from. Do NOT load any other framework document. (Recap: project references are the escape-hatch / nursery — the DEFAULT home for reference content is a skill's references/. The criteria check whether the content has matured past a standalone doc, serves one reading task, is reachable, and does not duplicate a skill.)
3. PLACEMENT (PD-2 maturation): judge whether the content has matured past a project doc. Three signals in leverage order — graduate to a skill (taxonomy B: a stabilized procedure / rule+counter / lookup table / tool-wrapper with a clear trigger), fold into a CLAUDE.md (taxonomy C: a small load-bearing tip), or move into an existing skill's references/ (taxonomy D: an existing skill owns the topic). If the content is genuinely emerging/unstructured, NONE fires and PD-2 PASSes. All maturation findings are JUDGMENT/DISCUSS — never FAIL a doc for not yet being a skill.
4. CRP (PD-3 + size): ${sizeClause}
5. ADP discoverability (PD-4): ${orphanClause}
6. ADP one-hop (PD-5): scan outbound doc-to-doc citations; a required reading CHAIN (A -> B -> C) is a FAIL (group ADP, taxonomy F_chained_reference). A single informational pointer to a sibling doc or SKILL.md is fine.
7. ADP back-reference (PD-6): if the body cites specific CLAUDE.md SECTION CONTENT as required reading (reversing load order), that is a FAIL (group ADP, taxonomy G_claude_md_back_reference). A pure orientation mention ("see the root CLAUDE.md") is permitted.
8. CCP (PD-8): if a skill already owns this doc's topic and the doc restates (rather than points at) that skill's content, that is a FAIL (group CCP, taxonomy I_duplicates_skill); INFO if the doc predates the skill and graduation is in progress.
9. Hygiene (PD-H1): outbound file-path links must resolve. A broken file-path reference is a FAIL (group Hygiene). Do NOT check /skill-name or skill:"..." link integrity — that is references-audit's job, out of scope here.
10. Classify EVERY non-PASS finding into a taxonomy id and a remediation bucket:
     - DISCUSS = needs a user decision (A, B, C, D, E, F, G, H, I, J)
     - SPECIAL = K, unclassified
   This audit has no AUTO bucket — every remediation is a structural move or a judgment the user confirms.
   PASS / INFO / JUDGMENT findings that need no remediation get taxonomy "none" and bucket "NONE".
   For each DISCUSS/SPECIAL finding write a concrete \`remediation\` (what you propose, with line refs).
11. Verdict: NON-COMPLIANT if ANY finding has severity FAIL; otherwise COMPLIANT. INFO/JUDGMENT never gate (maturation, orphan, and split-candidacy are all JUDGMENT — a useful-where-it-sits doc is COMPLIANT).

Idempotency matters: apply the fixed criteria and taxonomy deterministically. Do not invent findings; report only what the criteria actually surface. Return the structured object.`
}

phase('Audit')
const perFile = await parallel(input.files.map((f) => () =>
  // Default lane tier: opus at high effort. Detection is the audits' judgment
  // core — criteria application warrants the judge tier, explicitly pinned.
  agent(lanePrompt(f), {
    label: `audit:${f.path.split(/[\\/]/).pop()}`,
    phase: 'Audit',
    model: 'opus',
    effort: 'high',
    schema: FILE_FINDINGS_SCHEMA,
  }).then((r) => ({ ...r, path: f.path, kind: f.kind || 'project_doc' }))
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

log(`Audited ${results.length}/${input.files.length} project docs — ${totals.nonCompliant} NON-COMPLIANT, ${totals.fail} FAIL findings; buckets DISCUSS=${totals.discuss} SPECIAL=${totals.special}`)

return { perFile: results, totals }
