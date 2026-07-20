// claude-md-audit — DETECT workflow (before-Q&A phase).
//
// Fan-out detection + classification, one lane per target CLAUDE.md file. Each
// lane reads the file (and its parent, for child role), loads the SINGLE
// self-contained audit-criteria doc, applies the role-to-criteria map,
// optionally runs the mechanical schema validator, and classifies every finding
// into the taxonomy + a remediation bucket. Cache efficiency: each fan-out lane
// is an isolated context whose prompt prefix is NOT shared across siblings (the
// Workflow tool re-creates per-lane cache beyond a fixed harness shell), so the
// lane loads exactly ONE criteria doc -- the upstream content-allocation
// framework is the derivation, not the operative rules, and is intentionally not
// read here. Pure detection — NO file is modified here (the skill's
// `audit_then_self_remediate` anti-pattern keeps detection and remediation in
// separate phases). Returns structured per-file findings for the main loop to
// render and dispatch.
//
// Invoked by the claude-md-audit SKILL.md only when auditing 2+ files (the
// multi-file threshold that equalizes the Workflow tool's per-run overhead).
// Single-file audits run inline in the main loop.
//
// args = {
//   files: [ { path: string, role: "root"|"ancestor"|"child"|"local",
//              parentPath: string|null } ],
//   files[i].dimension: "code-directory" | "classic"  (from discover.py; when
//            "code-directory" the lane also loads refs.codeDirFilter and runs the
//            CD-* insight-validation criteria. Absent/"classic" -> classic only.)
//   density: boolean  (opt-in density lens. When true, every lane also loads
//            refs.densityCriteria and runs the DD-1..DD-4 lens, emitting findings
//            under group "Density" -- all JUDGMENT, disposition IMPROVE, never FAIL.
//            Absent/false -> the lens does not run and the doc is not loaded.)
//
// Per-finding disposition (FIX / SERIOUS / IMPROVE / SILENT; K -> SPECIAL) is
// assigned instance-level by the step-8 classifier, not fixed by the taxonomy
// default. Report contract for the main loop: SERIOUS summarized at the top,
// FIX as an applied count (lands in the remediation CL), IMPROVE as a count +
// one-line pitches (opt-in discussion), SILENT omitted entirely, no hedging.
//   refs:  { criteria: <abs path to references/audit-criteria.md>,
//            codeDirFilter: <abs path to references/code-dir-insight-filter.md>,
//            densityCriteria: <abs path to references/density-criteria.md>  (only used when density is true),
//            pluginRoot: <abs path to plugins/skills-kit (parent of skills_kit_lib)>,
//            venvPython: <abs path to skills-kit venv python> }
// }
// The schema validator is invoked as a module:
//   (cd <pluginRoot> && <venvPython> -m skills_kit_lib.audit <file> --json)

export const meta = {
  name: 'claude-md-audit-detect',
  description: 'Fan-out CLAUDE.md audit: read + apply CCP/CRP/ADP criteria + schema-validate + classify, one lane per file (detection only, no edits)',
  phases: [{ title: 'Audit', detail: 'one lane per CLAUDE.md file' }],
}

const FILE_FINDINGS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    path: { type: 'string' },
    role: { type: 'string', enum: ['root', 'ancestor', 'child', 'local'] },
    lines: { type: 'integer' },
    approx_tokens: { type: 'integer' },
    has_schema_block: { type: 'boolean' },
    parent_available: { type: 'boolean', description: 'true if a parent CLAUDE.md was read (child role); false/irrelevant otherwise' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          group: { type: 'string', enum: ['CCP', 'CRP', 'ADP', 'Hygiene', 'Schema', 'CodeDir', 'Density'] },
          severity: { type: 'string', enum: ['PASS', 'FAIL', 'INFO', 'JUDGMENT'] },
          criterion: { type: 'string', description: 'criterion id or short name, e.g. ccp_cross_file_duplication' },
          message: { type: 'string' },
          line: { type: ['integer', 'null'], description: 'line number in the file, or null' },
          taxonomy: { type: 'string', description: 'taxonomy id A-G, P, Q, or K; "none" for PASS/INFO/JUDGMENT that need no remediation' },
          bucket: { type: 'string', enum: ['FIX', 'SERIOUS', 'IMPROVE', 'SILENT', 'SPECIAL', 'NONE'], description: 'per-finding disposition assigned instance-level by the classifier (step 8)' },
          remediation: { type: 'string', description: 'concrete proposed remediation for AUTO/DISCUSS/SPECIAL; empty for NONE' },
        },
        required: ['group', 'severity', 'criterion', 'message', 'line', 'taxonomy', 'bucket', 'remediation'],
      },
    },
    verdict: { type: 'string', enum: ['COMPLIANT', 'NON-COMPLIANT'] },
  },
  required: ['path', 'role', 'lines', 'approx_tokens', 'has_schema_block', 'parent_available', 'findings', 'verdict'],
}

// args may arrive as an object or as a JSON string depending on how the
// invoker passes it; normalize to an object.
let input = args
if (typeof input === 'string') {
  try { input = JSON.parse(input) } catch (_) { input = null }
}
if (!input || !Array.isArray(input.files) || input.files.length === 0) {
  throw new Error('detect.js requires args.files = [{path, role, parentPath}]')
}
const refs = input.refs || {}
const density = input.density === true

function lanePrompt(f) {
  const densityClause = density
    ? `The OPT-IN density lens is requested. After the checks above, ALSO read the density criteria at ${refs.densityCriteria} and run the DD-1..DD-4 lens. Overriding rule: density != deletion — every finding must route the tokens somewhere (tighten in place / extract to a named reference / merge a duplicate); if you cannot name the destination, do not raise the finding. DD-1 density_in_place (over-worded but correctly-placed section -> taxonomy L_verbose_in_place, tighten IN PLACE, honor carve-outs for teaching examples / load-bearing nuance / labeled safety rails); DD-2 extract_to_reference (self-contained on-demand block taxing every reader -> taxonomy M_extract_to_reference, move to a reference + leave a one-line pointer; distinct from A wrong-scope and finer than C whole-file split); DD-3 intra_file_redundancy (same fact repeated within THIS file -> taxonomy N_intra_file_redundancy; NOT B, which is across the role chain); DD-4 value_earns_tokens (classic-file generalization of the CD-5 value filter -> taxonomy O_low_value_verbose; do NOT run on a code-directory file, where CD-5/J already owns value). Emit ALL density findings under group "Density", severity JUDGMENT, disposition IMPROVE — the density lens is the opt-in improvement lens (trims of true content passing the one-line test / structural moves), it NEVER produces FAIL and never changes the verdict. Each remediation names the destination (tighten | extract->ref | merge) and an approximate token-savings figure.`
    : `The density lens was not requested; do NOT load or apply the density criteria, and emit no Density-group findings.`

  const parentClause = f.role === 'child' && f.parentPath
    ? `This is a CHILD file. Also Read its parent CLAUDE.md at ${f.parentPath} so you can run the CCP cross-file duplication check (a rule restated from the parent is a FAIL, taxonomy B, disposition FIX under the loss-free-deletion guard + summarize-and-reference rule).`
    : `No parent read is required for role=${f.role}.`

  const codeDirClause = f.dimension === 'code-directory'
    ? `This file is flagged \`code-directory\` (it is per-directory review notes for code/YAML/CSV). After the classic checks, ALSO read the insight-validation criteria at ${refs.codeDirFilter} and run the CD-* dimension on it. The order is fixed: (a) identify the file's shape(s) A/B/C/D; (b) for EVERY concrete anchor a claim makes, classify its modality FIRST (requires-present / requires-absent / external-unverifiable / template-or-env / vendored-don't-read / generated-or-unsynced / non-anchor) — only \`requires-present\` is eligible for FAIL, and \`requires-absent\` is scored INVERTED (presence of the asserted-absent thing is the FAIL); (c) apply CD-2 fidelity_anchor_resolves (FAIL=H stale-anchor / H2 inverted-absence), CD-3 line-drift (I2, silent if the author gave a recovery hint), CD-4 claim_holds (I; counted magnitudes never FAIL), CD-5 value filter honoring every carve-out (J), CD-6 silent_failure_preserved (INFO). Assign each a disposition in step 8, not here (H re-points to a found mechanism -> FIX, or SERIOUS when it guards an invariant with no surviving mechanism; H2 -> SERIOUS, the invariant is violated; I2 -> FIX; I claim-drift verified from the code reading -> FIX; J default/bare-inventory/restatement -> FIX, true content passing the one-line test -> IMPROVE, validator-artifact/historical-record -> SILENT). Resolve symbol anchors repo-wide and leading-slash paths against repo root. Emit these under group "CodeDir". Validate existing claims only — do NOT crawl the directory for new gotchas (non-idempotent). NEVER FAIL an external/template/vendored/generated/non-anchor anchor.`
    : `This file is flagged \`classic\` — run the classic CCP/CRP/ADP/Hygiene/Schema criteria only; do NOT load or apply the code-directory insight filter.`

  const schemaClause = refs.pluginRoot && refs.venvPython
    ? `If the file body contains a \`claude_md:\` YAML contract block, run the mechanical schema validator via Bash (it is a package module, so cd into the plugin root first):\n    (cd "${refs.pluginRoot}" && "${refs.venvPython}" -m skills_kit_lib.audit "${f.path}" --json)\nand merge its results as Schema-group findings (validation failure on a non-optional field = FAIL, taxonomy E). If the validator is unavailable or errors, emit one Schema finding with severity JUDGMENT and message "schema validator unavailable" and continue. If there is no \`claude_md:\` block, skip the Schema group entirely (do NOT fail a file for not declaring a contract).`
    : `Schema validator path was not provided; if the file has a \`claude_md:\` block, emit one Schema finding with severity JUDGMENT noting the validator was unavailable.`

  return `You are ONE lane of a CLAUDE.md audit. Audit exactly one file and return structured findings. This is DETECTION ONLY — do not modify any file.

Target:    ${f.path}
Role:      ${f.role}
Dimension: ${f.dimension || 'classic'}

Steps:
1. Read the target file. Count its lines and estimate tokens (~chars/4).
2. Read the audit criteria and role-to-criteria map at ${refs.criteria}. This file is self-contained: every testable rule is stated together with the CCP / CRP / ADP principle it derives from. Do NOT load any other framework document -- everything needed to classify is in this one file. (Principle recap so you can apply them without re-derivation: CCP = content that changes for the same reason belongs together; a rule duplicated across scopes is a FAIL. CRP = a fact lives in the smallest scope whose readers all need it. ADP = cross-file references must resolve and run downward in load order; a broken or stale reference is a FAIL.)
3. ${parentClause}
4. Apply the criteria that the role-to-criteria map says apply to role=${f.role}. Produce findings tagged with group (CCP / CRP / ADP / Hygiene) and severity (PASS / FAIL / INFO / JUDGMENT). For role=local, only the D-group / local criteria apply (skip Hygiene and ADP per the map).
5. ${schemaClause}
6. ${codeDirClause}
7. ${densityClause}
8. DISPOSITION CLASSIFIER. Assign EVERY non-PASS finding a taxonomy id and one of four dispositions -- FIX / SERIOUS / IMPROVE / SILENT (K unclassified -> SPECIAL). The taxonomy default bucket is a starting point only; decide the disposition instance-level against these predicates.

   Classifier prod (read this FIRST -- it overrides your default caution): You are biased toward conservatism; the user's time and attention are the scarce resources; source control and CL review are the safety net. If the edit very likely improves the doc, apply it.

   Master razor: FIX = anything decidable by VERIFIED FACTS plus DOCUMENTED PROJECT CONVENTIONS. Reserve IMPROVE for where no fact and no convention decides. The bar for FIX is: would a reasonable owner, seeing this diff in CL review, accept it without discussion? "Very likely improving" clears it.

   FIX (auto-applied; lands in a reviewable CL) when the finding is:
     - a correction against a verified fact: a broken link/path with an identified target; a stale anchor re-pointed to the found mechanism (H); a count/list/attribution/signature (P); a semantic claim corrected from a verified code reading (I). Prefer the information-preserving fix -- update the count AND add the missing entry; never just drop the count.
     - deletion of FALSIFIED content -- a claim the world has disproven (an inventory entry for a directory that no longer exists). False guidance has negative value; deleting it loses nothing.
     - a convention-violation fix: a non-ASCII look-alike, a hardcoded absolute / foreign-machine path, a drifted line number (I2), or dedup under the summarize-and-reference rule (B cross-file duplication, Q skill-content duplication -- trim to a REMINDER PLUS REFERENCE: an inline summary of a dozen tokens or less plus the pointer to the SSOT, or reference-only beyond that budget).
     - a trim of a default or obvious-to-any-agent content (framework-default naming, "follow the same patterns" filler, a copy of --help output; bare un-annotated inventory J). Be aggressive here.
     Intent re-derivation is NOT a blocker when you verified the actual behavior from code -- the code reading is evidence; fix it. (Exception: a protective rail whose mechanism no longer exists -> SERIOUS.)
     Loss-free-deletion guard (procedural, ALWAYS before any FIX that removes a duplicate or section): diff it against the surviving copy and fold any local delta -- the extra fact, the directory-specific anchor -- into the SSOT or the summary line FIRST. Only then delete the proven-redundant remainder.
   SERIOUS (surface at the TOP of the report, summarized, NEVER auto-fixed, never buried mid-list) when the finding is: a secret / security finding; a protective rail whose documented mechanism turns out to be fictional (the real finding is the unprotected invariant, not the doc drift); or any case where the doc problem reveals a real-world problem (data-file collisions, out-of-scope fixes). CodeDir H2 (a requires-absent invariant now violated) is SERIOUS.
   IMPROVE (report as a count + one-liners; discussion is opt-in) when the finding is: a structural move (A wrong-role, C CRP split, D forward-dependency, F/G placement; Density L/M/N/O; graduate/fold/absorb/orphan-link/placement change); or a trim of TRUE content that passes the one-line test -- if the section's value can be stated in one line, offer the trim as a one-liner; if it cannot, do not offer it at all. E authorial-schema-choice and P-ambiguous (the discrepancy might be intentional) are IMPROVE.
   SILENT (do NOT surface at all; no hedging -- if the recommendation is do nothing, say nothing) when the finding is: a do-nothing conclusion ("no action required", "accept as-is -> PASS"); a validator detection artifact (a heuristic gap); or an accepted structural pattern (an agent-definition file with zero inbound citations, a historical record, a companion-source PDF).
   SPECIAL = K only (the genuine escape hatch: surface the row, the attempted matches, and why none fit; the user proposes a strategy).

   Ambiguity rulings (apply when the disposition is unclear):
   1. Your own verified code-reading DISCHARGES any "confirm with author" hedge -- if the lane verified the actual behavior from code, the correction is FIX; do not leak it back into discussion.
   2. A generator-owned path absent from the checkout (e.g. Docs/ConfigFormat/*, anything that materializes on doc-gen like Generated/) is NOT a broken link. Adding the annotation "auto-generated (present after doc-gen)" is FIX (additive, loses nothing, prevents future false flags); repointing or deleting such a reference stays IMPROVE.
   3. When a duplicate is both auto-dedupable and part of a larger opt-in relocation: FIX the dedup now (collapse to summary + reference); the relocation stays a separate IMPROVE. Dedup never waits on structure.
   4. A CRP/size split is offerable (IMPROVE) only when you can NAME a concrete extraction candidate; a bare over-threshold nudge with no named candidate is SILENT (mirror of the one-line trim test).
   5. A validator detection artifact is SILENT only when placating the validator needs no real doc change. If the same edit is ALSO a genuine project-convention fix (e.g. backslash paths -> forward slashes), it is FIX.

   Classic-dimension findings use taxonomy A-G/P/Q/K; CodeDir-group findings use H_stale_anchor / H2_inverted_absence / I_claim_drift / I2_line_drift / J_low_value_insight (or K); Density-group findings use L_verbose_in_place / M_extract_to_reference / N_intra_file_redundancy / O_low_value_verbose.
   Declined-opportunity ledger: if the target's frontmatter carries an \`md-audit-declined:\` list (bare taxonomy ids or short finding keys), do NOT re-raise an IMPROVE finding the user already declined for that file -- honor it exactly like references-audit honors \`references-audit-allow-stale\`. A new or materially different finding still fires.
   PASS / INFO / JUDGMENT findings that need no remediation get taxonomy "none" and bucket "NONE".
   For each FIX/SERIOUS/IMPROVE/SPECIAL finding write a concrete \`remediation\` (what edit you propose, with line refs); FIX writes the edit it will apply, SERIOUS writes the one-line summary for the top-of-report block, IMPROVE writes the single one-line pitch.
9. Verdict: NON-COMPLIANT if ANY finding has severity FAIL; otherwise COMPLIANT. INFO/JUDGMENT never gate. (A CodeDir CD-2 H/H2 FAIL gates exactly like a classic FAIL. Density findings are JUDGMENT only and never affect the verdict.) Disposition is orthogonal to the verdict -- a FIX still lands in the remediation CL, a SERIOUS still gates via its FAIL severity if it carries one.

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
  }).then((r) => ({ ...r, path: f.path, role: f.role, dimension: f.dimension || 'classic' }))
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

log(`Audited ${results.length}/${input.files.length} files — ${totals.nonCompliant} NON-COMPLIANT, ${totals.fail} FAIL findings; dispositions SERIOUS=${totals.serious} FIX=${totals.fix} IMPROVE=${totals.improve} (SILENT=${totals.silent} omitted)`)

return { perFile: results, totals }
