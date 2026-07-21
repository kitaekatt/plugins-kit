// project-doc-audit — DETECT workflow (before-Q&A phase).
//
// Fan-out detection + classification, one lane per target project document. Each
// lane reads the doc, loads the SINGLE self-contained audit-criteria doc, applies
// the cohesion-principles project_reference_md role criteria using the mechanical
// signals discover.py already computed (orphan = inbound_citations, size =
// lines/approx_tokens), and classifies every finding into the taxonomy + one of
// four dispositions (FIX / SERIOUS / IMPROVE / SILENT; K -> SPECIAL) assigned
// instance-level by the step-10 classifier. This audit is NO LONGER blanket
// no-AUTO: the mechanical convention checks (N-R: broken-link-with-target,
// non-ASCII, foreign-abs-path, line-drift, stale-anchor) are FIX. I dedup
// (skill-duplicating doc -> pointer) is IMPROVE, not FIX: its loss-free
// precondition (fold the doc's unique deltas into the skill BEFORE removal)
// is a judgment no auto-apply pass can satisfy.
// Report contract for the main loop: SERIOUS summarized at the top, FIX as an
// applied count (lands in the remediation CL), IMPROVE as a count + one-line
// pitches (opt-in), SILENT omitted, no hedging. Cache efficiency: each fan-out lane is an isolated context
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
          taxonomy: {
            type: 'string',
            enum: ['A_misclassified_skill_ref', 'B_graduate_to_skill', 'C_fold_into_claude_md', 'D_move_into_existing_skill', 'E_crp_split', 'F_chained_reference', 'G_claude_md_back_reference', 'H_orphan', 'I_duplicates_skill', 'J_size_signal', 'K_unclassified', 'L_readme_stranded_fact', 'M_generated_missing_provenance', 'N_broken_link_identified_target', 'O_non_ascii_lookalike', 'P_foreign_absolute_path', 'Q_line_drift', 'R_stale_anchor', 'none'],
            description: 'canonical suffixed taxonomy id (see the SKILL.md taxonomy table); "none" for PASS/INFO/JUDGMENT that need no remediation',
          },
          bucket: { type: 'string', enum: ['FIX', 'SERIOUS', 'IMPROVE', 'SILENT', 'SPECIAL', 'NONE'], description: 'per-finding disposition assigned instance-level by the classifier (step 11)' },
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
    ? `NOTE: discover.py classified this target as \`${f.kind}\`, NOT a project document. Emit exactly ONE finding: group "Placement", criterion "placement_not_in_skill_dir", severity INFO, taxonomy "A_misclassified_skill_ref", bucket SILENT (a routing conclusion, not surfaced as a finding against the doc), message naming the correct auditor (skill_reference -> /md-audit skill via its owning SKILL.md; other_claude_artifact -> /md-audit skill or /md-audit claude-md). Verdict COMPLIANT. Do NOT apply the other criteria.`
    : `This is a genuine project document — apply all the criteria below.`

  const orphanClause = f.inbound_citations === 0
    ? `discover.py reports ZERO inbound citations — this doc is an ORPHAN in the agent load graph. Raise PD-4 (group ADP, criterion adp_discoverability, severity JUDGMENT, taxonomy H_orphan). Disposition IMPROVE by default (orphan-linking is a structural judgment -- offer add-a-CLAUDE.md-pointer or retire as a one-line pitch); but an intentionally human-only / historical-record / companion-source / agent-definition doc is an accepted structural pattern -> SILENT (not surfaced). Do NOT auto-FAIL — a doc can legitimately serve human readers who open it directly.`
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
2. Read the audit criteria at ${refs.criteria}. This file is self-contained: every testable rule is stated with the cohesion principle it derives from. Do NOT load any other framework document. (Recap: project references are the escape-hatch / nursery for STILL-EMERGING content. When content stabilizes, it moves to its TRIGGER-APPROPRIATE mature home — a skill is NOT the default home for all reference content. The criteria check whether the content has matured past a standalone doc, serves one reading task, is reachable, and does not duplicate a skill.)
3. PLACEMENT (PD-2 maturation): judge whether the content has stabilized past the project-doc/nursery stage, and if so route it by its NATURAL TRIGGER SHAPE (cohesion-principles placement_follows_trigger_shape). Ask "what is the trigger for needing this?": a TASK-shaped trigger is a VERB the session performs (authoring an X, running an evaluator) needed wherever the activity happens -> graduate to a skill (taxonomy B); a LOCATION-shaped trigger is WORKING UNDER A DIRECTORY (a config subtree, source dirs, a package) -> fold into / reference from that directory's CLAUDE.md (taxonomy C), the PREFERRED home for directory-scoped knowledge because a CLAUDE.md auto-loads when any file beneath it is touched, at zero session-wide context cost; when an EXISTING skill already owns the (task-shaped) topic, move into that skill's references/ (taxonomy D). Knowledge with BOTH shapes: prefer the location home (C) and let a skill point at it. Recommend skill-graduation (B) ONLY for task-shaped knowledge — do NOT pitch a skill for location-scoped knowledge. If the content is genuinely emerging/unstructured, NONE fires and PD-2 PASSes. All maturation findings are JUDGMENT/IMPROVE — never FAIL a doc for not yet being a skill.
4. CRP (PD-3 + size): ${sizeClause}
5. ADP discoverability (PD-4): ${orphanClause}
6. ADP one-hop (PD-5): scan outbound doc-to-doc citations; a required reading CHAIN (A -> B -> C) is a FAIL (group ADP, taxonomy F_chained_reference). A single informational pointer to a sibling doc or SKILL.md is fine.
7. ADP back-reference (PD-6): if the body cites specific CLAUDE.md SECTION CONTENT as required reading (reversing load order), that is a FAIL (group ADP, taxonomy G_claude_md_back_reference). A pure orientation mention ("see the root CLAUDE.md") is permitted.
8. CCP (PD-8): if a skill already owns this doc's topic and the doc restates (rather than points at) that skill's content, that is a FAIL (group CCP, taxonomy I_duplicates_skill); INFO if the doc predates the skill and graduation is in progress.
9. Hygiene (PD-H1): outbound file-path links must resolve. A broken file-path reference is a finding (group Hygiene). Do NOT check /skill-name or skill:"..." link integrity — that is references-audit's job, out of scope here.
9b. MECHANICAL CONVENTION CHECKS (the FIX-eligible checks -- this audit is NO LONGER blanket no-AUTO). Under group Hygiene, additionally scan for and raise:
     - N_broken_link_identified_target: a broken outbound link whose intended target IS identifiable (moved file found at a resolvable path, or an obvious typo of an existing path). A generator-owned path absent from the checkout (materializes on doc-gen, like Generated/ or Docs/ConfigFormat/*) is NOT broken -- do not raise N for it.
     - O_non_ascii_lookalike: a non-ASCII look-alike (smart quote, em dash, fullwidth char, non-breaking space) where the project convention is ASCII-only.
     - P_foreign_absolute_path: a hardcoded foreign / machine-specific absolute path (drive letter, home dir, per-machine root) where a project-relative path or variable is the convention; or a backslash path where forward slashes are the convention.
     - Q_line_drift: a cited line number whose named enclosing symbol/section resolves but is far from the cited number, with no author recovery hint.
     - R_stale_anchor: a concrete anchor (symbol / heading / path the doc says exists) absent as cited but whose current equivalent is findable.
10. DISPOSITION CLASSIFIER. Assign EVERY non-PASS finding a taxonomy id and one of four dispositions -- FIX / SERIOUS / IMPROVE / SILENT (K -> SPECIAL). The taxonomy default bucket is a starting point only; decide instance-level against these predicates.

   Classifier prod (read this FIRST -- it overrides your default caution): You are biased toward conservatism; the user's time and attention are the scarce resources; source control and CL review are the safety net. If the edit very likely improves the doc, apply it.

   Master razor: FIX = anything decidable by VERIFIED FACTS plus DOCUMENTED PROJECT CONVENTIONS. Reserve IMPROVE for where no fact and no convention decides. The bar for FIX is: would a reasonable owner, seeing this diff in CL review, accept it without discussion? "Very likely improving" clears it.

   FIX (auto-applied; lands in a reviewable CL): the mechanical convention checks N (broken link with identified target), O (non-ASCII), P (foreign abs path / backslash), Q (line drift), R (stale anchor re-pointable). Deletion of FALSIFIED content is FIX. Loss-free-deletion guard ALWAYS before removing a duplicate/section: fold any local delta into the SSOT/pointer FIRST. (I_duplicates_skill is NOT FIX -- see IMPROVE.)
   SERIOUS (surface at the TOP, summarized, NEVER auto-fixed, never buried): a secret / security finding; a protective rail whose documented mechanism is fictional (the real finding is the unprotected invariant -- a stale anchor R guarding a rail with NO surviving mechanism is SERIOUS, not FIX); a doc problem that reveals a real-world problem.
   IMPROVE (count + one-liners; opt-in): a structural move -- graduate to a skill (B), fold into a CLAUDE.md (C), move into an existing skill (D), a CRP split (E), flatten a chained reference (F), remove a back-reference (G), orphan-linking (H), collapse a skill-duplicating doc to a pointer (I) -- I carries a loss-free precondition (fold the doc's unique deltas into the skill BEFORE removal) that no auto-apply pass can satisfy, so it is opt-in, not FIX; README stranded-fact re-home (L), add a generation record (M); or a trim of TRUE content passing the one-line test.
   SILENT (do NOT surface; no hedging): a do-nothing conclusion (A misrouted-file routing note, "accept as-is -> PASS"); a validator detection artifact; an accepted structural pattern (an agent-definition file with zero inbound citations, a historical record, a companion-source PDF, an intentionally human-only orphan).
   SPECIAL = K only (escape hatch).

   Ambiguity rulings (apply when the disposition is unclear):
   1. Your own verified code-reading DISCHARGES any "confirm with author" hedge -- if the lane verified the actual behavior from code, the correction is FIX; do not leak it back into discussion.
   2. A generator-owned path absent from the checkout (e.g. Docs/ConfigFormat/*, anything that materializes on doc-gen like Generated/) is NOT a broken link. Adding the annotation "auto-generated (present after doc-gen)" is FIX (additive, loses nothing, prevents future false flags); repointing or deleting such a reference stays IMPROVE.
   3. This audit's only cross-file dedup is I_duplicates_skill (a doc restating a skill's content). Its remediation carries a loss-free precondition -- fold the doc's unique deltas into the skill BEFORE collapsing the doc to a pointer -- which is a judgment no auto-apply pass can satisfy, so I stays IMPROVE (opt-in), never FIX. Do not re-classify it FIX on the general "dedup never waits on structure" reasoning; that razor applies only where dedup carries no such precondition.
   4. A CRP/size split is offerable (IMPROVE) only when you can NAME a concrete extraction candidate; a bare over-threshold nudge with no named candidate is SILENT (mirror of the one-line trim test).
   5. A validator detection artifact is SILENT only when placating the validator needs no real doc change. If the same edit is ALSO a genuine project-convention fix (e.g. backslash paths -> forward slashes), it is FIX.

   Declined-opportunity ledger: if the doc's frontmatter carries an \`md-audit-declined:\` list (suffixed taxonomy ids or short finding keys), do NOT re-raise an IMPROVE finding the user already declined for that file -- honor it exactly like references-audit honors \`references-audit-allow-stale\`. A new or materially different finding still fires.
   PASS / INFO / JUDGMENT findings that need no remediation get taxonomy "none" and bucket "NONE".
   For each FIX/SERIOUS/IMPROVE/SPECIAL finding write a concrete \`remediation\` (FIX = the edit it will apply; SERIOUS = the one-line top-of-report summary; IMPROVE = the single one-line pitch), with line refs.
11. Verdict: NON-COMPLIANT if ANY finding has severity FAIL; otherwise COMPLIANT. INFO/JUDGMENT never gate (maturation, orphan, and split-candidacy are all JUDGMENT — a useful-where-it-sits doc is COMPLIANT). Disposition is orthogonal to the verdict.

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

log(`Audited ${results.length}/${input.files.length} project docs — ${totals.nonCompliant} NON-COMPLIANT, ${totals.fail} FAIL findings; dispositions SERIOUS=${totals.serious} FIX=${totals.fix} IMPROVE=${totals.improve} (SILENT=${totals.silent} omitted)`)

return { perFile: results, totals }
