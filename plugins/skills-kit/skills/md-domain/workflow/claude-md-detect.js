// md-domain audit_claude_md lane — DETECT workflow (before-Q&A phase).
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
// Invoked by the md-domain SKILL.md (audit_claude_md lane) when auditing 2+ files (the
// multi-file threshold that equalizes the Workflow tool's per-run overhead).
// Single-file audits normally run inline in the main loop -- EXCEPT in review
// mode, where the threshold drops to 1 so every review-mode detect goes through
// a lane. Review mode exists to gate a submit/publish, so it cannot inherit the
// session model off the main loop; the lane is what pins model+effort and
// enforces the schema. See args.review below.
//
// args = {
//   files: [ { path: string, role: "root"|"ancestor"|"child"|"local",
//              parentPath: string|null, kind?: string } ],
//     (kind is the caller's artifact classification. "claude-md" -> apply the
//     criteria; any OTHER explicit value -> decline with NOT-AUDITED; ABSENT ->
//     the lane self-applies the CLAUDE.md / CLAUDE.local.md basename shape test
//     and declines on a non-match. See the decline contract in
//     references/lanes/audit-lane.md step 2a.)
//   review: boolean  (REVIEW MODE. When true, each finding is additionally
//            marked `attributable` -- whether the change under review caused it
//            -- via a targeted per-finding check against the pre-image. Lanes
//            stay mode-agnostic otherwise: they do NOT filter and do NOT change
//            the verdict. The caller filters on `attributable` and relabels the
//            verdict DIFF-CLEAN. See files[i].preImagePath.)
//   files[i].preImagePath: string|null  (review mode only. Absolute path to a
//            materialized copy of the file as it was BEFORE the change under
//            review. The CALLER materializes it -- `p4 print //path#have`, or
//            `git show <base>:<path>` -- because this plugin is VCS-agnostic and
//            must not learn Perforce or git. null means the file is an ADD with
//            no pre-image, in which case every finding is attributable.)
//   files[i].parentPreImagePath: string|null  (review mode, child role only.
//            Pre-image of the parent CLAUDE.md, needed so cross-file duplication
//            the change itself introduced in the PARENT is not misattributed to
//            the untouched child. null -> judge B against the current parent.)
//   files[i].ancestorClaudeMdPaths: string[]|undefined  (H-11 ancestor-convention
//            check. The FULL ancestor CLAUDE.md chain above the subject on the
//            directory path to the workspace root, nearest-ancestor first,
//            EXCLUDING the subject itself. Deliberately INCLUDES the parent that
//            parentPath names (the two overlap on the nearest ancestor -- they
//            drive different criteria: parentPath -> B textual duplication,
//            ancestorClaudeMdPaths -> H-11 declared-convention conformance -- so
//            no dedup is needed). When present and non-empty the lane reads these
//            files, extracts EXPLICITLY declared conventions, and checks the
//            subject against them under criterion H-11 (group Hygiene, taxonomy
//            R_ancestor_convention_violation). When absent/empty NO H-11 finding
//            is emitted.)
//   files[i].dimension: "code-directory" | "classic"  (from discover_claude_md.py; when
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
//   refs:  { criteria: <abs path to references/standards/claude-md-standards.md>,
//            codeDirFilter: <abs path to references/standards/claude-md-standards.md (section 3, the code-directory dimension)>,
//            densityCriteria: <abs path to references/standards/claude-md-standards.md (section 4, the density lens)>  (only used when density is true),
//            pluginRoot: <abs path to plugins/skills-kit (parent of skills_kit_lib)>,
//            venvPython: <abs path to skills-kit venv python> }
// }
// The schema validator is invoked as a module:
//   (cd <pluginRoot> && <venvPython> -m skills_kit_lib.audit <file> --json --config)
//   --config makes audit.py honor the resolved standards config (drop disabled
//   mechanical rows, overlay thresholds); disabled ids also arrive as
//   args.disabledCriteria and standards files per-file as files[i].standardsPaths.

export const meta = {
  name: 'md-domain-claude-md-detect',
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
          taxonomy: {
            type: 'string',
            enum: ['A_wrong_role_content', 'B_ccp_cross_file_duplication', 'C_crp_split_candidate', 'D_adp_forward_dependency', 'E_schema_failure', 'F_hygiene_threshold', 'G_descendant_role_mismatch', 'H_stale_anchor', 'H2_inverted_absence', 'I_claim_drift', 'I2_line_drift', 'J_low_value_insight', 'K_unclassified', 'L_verbose_in_place', 'M_extract_to_reference', 'N_intra_file_redundancy', 'O_low_value_verbose', 'P_stale_factual_claim', 'Q_skill_content_duplication', 'R_ancestor_convention_violation', 'N_user_standard_violation', 'none'],
            description: 'canonical suffixed taxonomy id (see the taxonomy table in references/standards/claude-md-standards.md); "none" for PASS/INFO/JUDGMENT that need no remediation, and for the artifact-shape decline finding',
          },
          bucket: { type: 'string', enum: ['FIX', 'SERIOUS', 'IMPROVE', 'SILENT', 'SPECIAL', 'NONE'], description: 'per-finding disposition assigned instance-level by the classifier (step 8)' },
          remediation: { type: 'string', description: 'concrete proposed remediation for FIX/SERIOUS/IMPROVE/SPECIAL; empty for SILENT/NONE' },
          attributable: { type: 'boolean', description: 'review mode: did the change under review cause this finding? Judged against the pre-image (step 8.5). ALWAYS true outside review mode -- nothing is being diffed, so every finding counts.' },
        },
        required: ['group', 'severity', 'criterion', 'message', 'line', 'taxonomy', 'bucket', 'remediation', 'attributable'],
      },
    },
    verdict: { type: 'string', enum: ['COMPLIANT', 'NON-COMPLIANT', 'NOT-AUDITED'], description: 'NOT-AUDITED = the criteria were never applied because the file is not a CLAUDE.md / CLAUDE.local.md (the artifact-shape decline). It is NOT a passing verdict and must never be reported as one.' },
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
  throw new Error('claude-md-detect.js requires args.files = [{path, role, parentPath}]')
}
const refs = input.refs || {}
const density = input.density === true
const review = input.review === true

function lanePrompt(f) {
  // The decline contract (audit-lane.md step 2a), generalized from PD-1. The
  // decline instruction must be reachable BOTH when the caller classified the
  // file (own-lane path: discover_claude_md.py ran) AND when no kind was supplied
  // (review-mode subject-lens call). Asserting "genuine CLAUDE.md" on a kind-less
  // call re-opens the fake gate: the lane gets told to audit a file its criteria
  // exclude, and DIFF-CLEAN comes back for a file nobody meaningfully read.
  const declineInstruction = `Emit exactly ONE finding: group "Schema", criterion "artifact_shape_not_claude_md", severity INFO, taxonomy "none", bucket IMPROVE (this is the one INFO finding that is NOT bucket NONE -- it is a routing conclusion carrying a remediation, not a finding against the file), message naming the correct lane (a \`SKILL.md\` -> \`/md-domain audit skill\`; a project document, including a skill's own references/*.md -> \`/md-domain audit project-doc\`) and stating plainly that THIS FILE WAS NOT AUDITED, with a \`remediation\` naming the lane to re-run it under. Verdict NOT-AUDITED. Do NOT apply the other criteria and do NOT edit anything. The finding is deliberately NOT SILENT: it is the caller's only signal that nothing read this file, and suppressing it next to a passing verdict is what makes a declined file read as an audited one. It is never suppressed in either mode; in review mode set \`attributable: true\` on it and skip the attributability check entirely.`
  const routingClause = f.kind && f.kind !== 'claude-md'
    ? `NOTE: the caller classified this target as \`${f.kind}\`, NOT a CLAUDE.md. ${declineInstruction}`
    : f.kind === 'claude-md'
      ? `This is a genuine CLAUDE.md — apply all the criteria below.`
      : `No \`kind\` signal was provided (typical for a review-mode subject-lens call, where the caller does not classify). Run the artifact-shape test YOURSELF FIRST: this lane audits files whose BASENAME is \`CLAUDE.md\` or \`CLAUDE.local.md\`. A \`SKILL.md\`, a \`references/*.md\` under a skill, or any other standalone document is NOT one -- if the basename is neither: ${declineInstruction} Otherwise treat it as a genuine CLAUDE.md and apply all the criteria below.`

  const densityClause = density
    ? `The OPT-IN density lens is requested. After the checks above, ALSO read the density criteria at ${refs.densityCriteria} and run the DD-1..DD-4 lens. Overriding rule: density != deletion — every finding must route the tokens somewhere (tighten in place / extract to a named reference / merge a duplicate); if you cannot name the destination, do not raise the finding. DD-1 density_in_place (over-worded but correctly-placed section -> taxonomy L_verbose_in_place, tighten IN PLACE, honor carve-outs for teaching examples / load-bearing nuance / labeled safety rails); DD-2 extract_to_reference (self-contained on-demand block taxing every reader -> taxonomy M_extract_to_reference, move to a reference + leave a one-line pointer; distinct from A wrong-scope and finer than C whole-file split); DD-3 intra_file_redundancy (same fact repeated within THIS file -> taxonomy N_intra_file_redundancy; NOT B, which is across the role chain); DD-4 value_earns_tokens (classic-file generalization of the CD-5 value filter -> taxonomy O_low_value_verbose; do NOT run on a code-directory file, where CD-5/J already owns value). Emit ALL density findings under group "Density", severity JUDGMENT, disposition IMPROVE — the density lens is the opt-in improvement lens (trims of true content passing the one-line test / structural moves), it NEVER produces FAIL and never changes the verdict. Each remediation names the destination (tighten | extract->ref | merge) and an approximate token-savings figure.`
    : `The density lens was not requested; do NOT load or apply the density criteria, and emit no Density-group findings.`

  const reviewClause = !review
    ? `Not review mode. Set \`attributable: true\` on EVERY finding -- nothing is being diffed, so every finding counts. Do not read any pre-image.`
    : f.preImagePath
      ? `REVIEW MODE. This audit gates a submit, so it must report only what the change under review actually caused. For EACH non-PASS finding you produced above, run a TARGETED check against the pre-image at ${f.preImagePath} (the file as it was BEFORE the change): does this same criterion fire at this same anchor in the pre-image?
   - Fires in the pre-image too -> \`attributable: false\` (pre-existing; not this change's doing).
   - Does not fire in the pre-image -> \`attributable: true\`.
   Do NOT re-run the whole audit on the pre-image. Ask one narrow factual question per finding; that is cheaper and far more stable than differencing two full reports.
   Match on (criterion, taxonomy, normalized anchor) -- NEVER on line number or message wording. Line numbers shift and phrasing varies; a finding that moved or got reworded is the SAME finding. When a pre-image finding plausibly corresponds to this one, be GENEROUS and call it non-attributable: a false "pre-existing" is a missed nag, a false "attributable" is an accusation the author cannot act on.
   PASS / INFO / NONE findings are ALWAYS \`attributable: true\` -- they carry no remediation, so "did the change cause it" is not a meaningful question and a \`false\` there would silently delete the row from the report.
   If the pre-image cannot be read (missing, empty, unreadable), do NOT guess: set \`attributable: true\` on every finding and add one JUDGMENT finding, group Hygiene, taxonomy "none", bucket "NONE", message "pre-image unreadable -- findings are unfiltered". Over-reporting is the safe direction; silently suppressing is not.
   Attributable means CAUSED BY, not LOCATED IN. A finding anchored far from the edited lines is still attributable if the change created it -- e.g. the change adds a rule that now duplicates one 200 lines up, and the B finding anchors on the older copy. Judge causation, not proximity.
   You still report EVERY finding with its real disposition. Do NOT drop, downgrade, or re-bucket anything based on attributability -- the caller filters. ${f.parentPreImagePath ? `For the CCP cross-file (B) check specifically, judge duplication against the PARENT PRE-IMAGE at ${f.parentPreImagePath}, not the current parent -- otherwise duplication this same change introduced in the parent gets misattributed to this untouched child.` : ''}`
      : `REVIEW MODE, and this file has NO pre-image (it is an ADD introduced by the change under review). Every finding is therefore caused by this change: set \`attributable: true\` on ALL of them. Do not look for a pre-image.`

  const parentClause = f.role === 'child' && f.parentPath
    ? `This is a CHILD file. Also Read its parent CLAUDE.md at ${f.parentPath} so you can run the CCP cross-file duplication check (a rule restated from the parent is a FAIL, taxonomy B, disposition FIX under the loss-free-deletion guard + summarize-and-reference rule).`
    : `No parent read is required for role=${f.role}.`

  const ancestorPaths = Array.isArray(f.ancestorClaudeMdPaths) ? f.ancestorClaudeMdPaths : []
  const ancestorConventionsClause = ancestorPaths.length > 0
    ? `H-11 ANCESTOR-DECLARED CONVENTIONS. Read each ancestor CLAUDE.md, nearest-ancestor first: ${ancestorPaths.map((p) => `"${p}"`).join(', ')}. These load ambient in any session that touches the subject, so a convention they EXPLICITLY declare (ASCII-only mandates, "no absolute paths in shared files", stated formatting/structure rules) binds the subject file too. For each such convention, check whether the subject VIOLATES it.
   Rule-extraction posture (mirror the code-review reviewer_a): flag a violation ONLY when you can quote the exact declared rule VERBATIM from an ancestor. No inferred conventions, no generic best-practice, no "spirit of" a rule, no convention you believe is standard but the ancestor did not write down. If you cannot quote the ancestor's rule text verbatim, do NOT raise the finding.
   Emit each violation as group "Hygiene", taxonomy R_ancestor_convention_violation, severity FAIL, anchored on the SUBJECT line that violates the rule. The \`message\` MUST carry (a) the verbatim ancestor rule quote and (b) the source path of the ancestor CLAUDE.md that declared it. Disposition is assigned in step 8 like any other convention-violation fix -- normally FIX (a mechanical correction against a documented project convention), SERIOUS when the violation reveals a real-world problem the rule exists to prevent (e.g. a committed secret an ancestor forbids).
   EXCEPTION AWARENESS: a convention an ancestor declares may itself carry an EXPLICIT scoped exception (e.g. "ASCII only, EXCEPT developer names in the contributors section may contain non-ASCII characters"). When an ancestor's exception COVERS the specific instance -- right file scope AND right content kind -- that instance is NOT a violation: do NOT emit the R_ancestor_convention_violation finding for it. The exception must be written down and actually cover this instance (same verbatim-quote posture -- no inferred or stretched exceptions; when in doubt the rule still binds and the finding fires). PRECEDENCE: the SAME declared rule + exception also governs the step-8 built-in universal-convention FIX (non-ASCII / hardcoded-path); an exception that silences THIS H-11 finding silences that built-in FIX too, and vice versa -- the two must never contradict on a given instance.
   Note: the nearest ancestor may be the same file as the parent read above; that is fine -- the B check reads it for duplication, this check reads it for declared conventions.`
    : `No ancestor CLAUDE.md paths were supplied; do NOT run the H-11 ancestor-convention check and emit no R_ancestor_convention_violation findings.`

  const builtinConventionExceptionClause = ancestorPaths.length > 0
    ? ` ANCESTOR-DECLARED EXCEPTION CARVE-OUT: the ancestor CLAUDE.md files supplied for step 3.5 may EXPLICITLY declare a SCOPED EXCEPTION to one of these universal conventions -- e.g. "ASCII only, EXCEPT developer names in the contributors section may contain non-ASCII characters". Before emitting a convention-violation FIX for a non-ASCII look-alike or a hardcoded absolute / foreign-machine path, check those ancestors for an explicit exception that COVERS this exact instance -- the right file scope AND the right content kind. If one does, do NOT emit the FIX: demote the finding to PASS (taxonomy "none", bucket "NONE") -- or INFO if it is worth noting -- and put the verbatim quoted exception rule plus the ancestor source path in its \`message\`. Same verbatim-quote posture as H-11: the exception must be written down and actually cover this instance; no inferred, generic, or stretched exceptions, and when in doubt the built-in check STILL fires. PRECEDENCE: this carve-out and H-11 (step 3.5) read the SAME ancestor declarations, so a declared rule + its exception must yield ONE consistent outcome for a given instance -- an exception that silences the H-11 R_ancestor_convention_violation finding silences this built-in convention FIX too, and vice versa; they must never contradict (H-11 silent while the built-in FIX still fires is exactly the bug this carve-out removes).`
    : ``

  const standardsPaths = Array.isArray(f.standardsPaths) ? f.standardsPaths : []
  const standardsClause = standardsPaths.length > 0
    ? `USER-AUTHORED STANDARDS. Read each standards file, nearest-layer first: ${standardsPaths.map((p) => `"${p}"`).join(', ')}. Each is a *-standards.md carrying a fenced \`standards_set:\` block whose \`criteria[]\` are the project's or user's own opinions for this artifact type. Apply ONLY criteria whose \`statement\` you can quote VERBATIM from the standards file -- same rule-extraction posture as the ancestor-convention check: no inferred rules, no generic best-practice, no "spirit of" a criterion; if you cannot quote the statement verbatim, do NOT raise the finding. SKIP any criterion whose \`enforcement\` is \`mechanical\` -- those are the audit.py validator's job (it runs them under --config), not yours; you evaluate only judgment criteria (enforcement \`judgment\` or absent). For each violated criterion emit group "Hygiene", taxonomy N_user_standard_violation, severity taken from the criterion's declared \`severity\` (fail -> FAIL, info -> INFO, judgment -> JUDGMENT), anchored on the subject line that violates it. The \`message\` MUST carry (a) the verbatim criterion statement, (b) the criterion \`id\`, and (c) the source standards-file path. Disposition is assigned in step 8 from the severity: a fail-severity violation is SERIOUS (a hard user-declared rule the auditor cannot mechanically satisfy -- surface at the top, never auto-fix), an info-severity note is IMPROVE (one-line pitch), a judgment-severity call is JUDGMENT (surfaced for review).`
    : `No user-authored standards files were supplied; do NOT apply any user standards and emit no N_user_standard_violation findings.`

  const disabledCriteria = Array.isArray(input.disabledCriteria) ? input.disabledCriteria : []
  const disabledClause = disabledCriteria.length > 0
    ? `DISABLED CRITERIA. The run configuration switched these optional criterion/rule ids OFF: ${disabledCriteria.map((d) => `"${d}"`).join(', ')}. SUPPRESS any finding whose criterion id or rule id matches one in that list -- do not emit it and do not count it toward the verdict. That list only ever names OPTIONAL ids; architectural (schema/contract) and integrity (frontmatter, reachability, convention) checks are NEVER in it, so never suppress one of those on account of this list.`
    : `No criteria were disabled for this run; apply every criterion normally.`

  const codeDirClause = f.dimension === 'code-directory'
    ? `This file is flagged \`code-directory\` (it is per-directory review notes for code/YAML/CSV). After the classic checks, ALSO read the insight-validation criteria at ${refs.codeDirFilter} and run the CD-* dimension on it. The order is fixed: (a) identify the file's shape(s) A/B/C/D; (b) for EVERY concrete anchor a claim makes, classify its modality FIRST (requires-present / requires-absent / external-unverifiable / template-or-env / vendored-don't-read / generated-or-unsynced / non-anchor) — only \`requires-present\` is eligible for FAIL, and \`requires-absent\` is scored INVERTED (presence of the asserted-absent thing is the FAIL); (c) apply CD-2 fidelity_anchor_resolves (FAIL=H stale-anchor / H2 inverted-absence), CD-3 line-drift (I2, silent if the author gave a recovery hint), CD-4 claim_holds (I; counted magnitudes never FAIL), CD-5 value filter honoring every carve-out (J), CD-6 silent_failure_preserved (INFO). Assign each a disposition in step 8, not here (H re-points to a found mechanism -> FIX, or SERIOUS when it guards an invariant with no surviving mechanism; H2 -> SERIOUS, the invariant is violated; I2 -> FIX; I claim-drift verified from the code reading -> FIX; J default/bare-inventory/restatement -> FIX, true content passing the one-line test -> IMPROVE, validator-artifact/historical-record -> SILENT). Resolve symbol anchors repo-wide and leading-slash paths against repo root. Emit these under group "CodeDir". Validate existing claims only — do NOT crawl the directory for new gotchas (non-idempotent). NEVER FAIL an external/template/vendored/generated/non-anchor anchor.`
    : `This file is flagged \`classic\` — run the classic CCP/CRP/ADP/Hygiene/Schema criteria only; do NOT load or apply the code-directory insight filter.`

  const schemaClause = refs.pluginRoot && refs.venvPython
    ? `If the file body contains a \`claude_md:\` YAML contract block, run the mechanical schema validator via Bash (it is a package module, so cd into the plugin root first):\n    (cd "${refs.pluginRoot}" && "${refs.venvPython}" -m skills_kit_lib.audit "${f.path}" --json --config)\n(--config makes audit.py drop disabled mechanical rows and overlay the resolved thresholds; the disabled ids also arrive as args.disabledCriteria, so honor both.) and merge its results as Schema-group findings (validation failure on a non-optional field = FAIL, taxonomy E). If the validator is unavailable or errors, emit one Schema finding with severity JUDGMENT and message "schema validator unavailable" and continue. If there is no \`claude_md:\` block, skip the Schema group entirely (do NOT fail a file for not declaring a contract).`
    : `Schema validator path was not provided; if the file has a \`claude_md:\` block, emit one Schema finding with severity JUDGMENT noting the validator was unavailable.`

  return `You are ONE lane of a CLAUDE.md audit. Audit exactly one file and return structured findings. This is DETECTION ONLY — do not modify any file.

Target:    ${f.path}
Role:      ${f.role}
Dimension: ${f.dimension || 'classic'}

${routingClause}

Steps:
1. Read the target file. Count its lines and estimate tokens (~chars/4).
2. Read the audit criteria and role-to-criteria map at ${refs.criteria}. This file is self-contained: every testable rule is stated together with the CCP / CRP / ADP principle it derives from. Do NOT load any other framework document -- everything needed to classify is in this one file. (Principle recap so you can apply them without re-derivation: CCP = content that changes for the same reason belongs together; a rule duplicated across scopes is a FAIL. CRP = a fact lives in the smallest scope whose readers all need it. ADP = cross-file references must resolve and run downward in load order; a broken or stale reference is a FAIL.)
3. ${parentClause}
3.5. ${ancestorConventionsClause}
3.6. ${standardsClause}
3.7. ${disabledClause}
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
     - a convention-violation fix: a non-ASCII look-alike, a hardcoded absolute / foreign-machine path, a drifted line number (I2), or dedup under the summarize-and-reference rule (B cross-file duplication, Q skill-content duplication -- trim to a REMINDER PLUS REFERENCE: an inline summary of a dozen tokens or less plus the pointer to the SSOT, or reference-only beyond that budget).${builtinConventionExceptionClause}
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

   Classic-dimension findings use taxonomy A-G/P/Q/K; the H-11 ancestor-convention finding (step 3.5) uses R_ancestor_convention_violation (group Hygiene); the user-authored-standards finding (step 3.6) uses N_user_standard_violation (group Hygiene, disposition driven by the criterion's declared severity -- fail -> SERIOUS, info -> IMPROVE, judgment -> JUDGMENT); CodeDir-group findings use H_stale_anchor / H2_inverted_absence / I_claim_drift / I2_line_drift / J_low_value_insight (or K); Density-group findings use L_verbose_in_place / M_extract_to_reference / N_intra_file_redundancy / O_low_value_verbose.
   Declined-opportunity ledger: if the target's frontmatter carries an \`md-audit-declined:\` list (suffixed taxonomy ids or short finding keys), do NOT re-raise an IMPROVE finding the user already declined for that file -- honor it exactly like references-audit honors \`references-audit-allow-stale\`. A new or materially different finding still fires.
   PASS / INFO / JUDGMENT findings that need no remediation get taxonomy "none" and bucket "NONE".
   For each FIX/SERIOUS/IMPROVE/SPECIAL finding write a concrete \`remediation\` (what edit you propose, with line refs); FIX writes the edit it will apply, SERIOUS writes the one-line summary for the top-of-report block, IMPROVE writes the single one-line pitch.
8.5. ATTRIBUTABILITY. ${reviewClause}
9. Verdict: NOT-AUDITED if the artifact-shape decline fired (the target is not a CLAUDE.md / CLAUDE.local.md, so the criteria were never applied) — checked FIRST and overriding everything else, in BOTH modes; never COMPLIANT or DIFF-CLEAN, which would assert a clean file nobody read. Otherwise NON-COMPLIANT if ANY finding has severity FAIL; otherwise COMPLIANT. INFO/JUDGMENT never gate. (A CodeDir CD-2 H/H2 FAIL gates exactly like a classic FAIL. Density findings are JUDGMENT only and never affect the verdict.) Disposition is orthogonal to the verdict -- a FIX still lands in the remediation CL, a SERIOUS still gates via its FAIL severity if it carries one.

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

const raw = perFile.filter(Boolean)

// Review mode owns the filter and the relabel -- NOT the lanes. Lanes emit every
// finding plus `attributable`; the reducer decides what survives and what the
// verdict is called. Keeping this out of the lane is what lets one lane prompt
// serve both modes.
//
// SERIOUS ALWAYS SURVIVES, attributable or not. A secret, or an invariant the
// docs claim is protected but isn't, is not the author's doing and is still the
// most important thing on the page. Filtering it because "the diff didn't cause
// it" would turn review mode into a way to walk past exactly the findings that
// most need a human.
const isKept = (fnd) => !review || fnd.attributable !== false || fnd.bucket === 'SERIOUS'

const results = raw.map((r) => {
  if (!review) return r
  // A DECLINED file is never relabelled. When a lane judges the file outside its
  // criteria's scope it never applied them, so neither verdict vocabulary is
  // available -- and the routing finding explaining the decline must survive
  // regardless of attributability, since it is the only record that nothing was
  // read. Relabelling this DIFF-CLEAN is the fake gate: a caller reads "audited,
  // no failure" where the truth is "declined, not my department".
  if (r.verdict === 'NOT-AUDITED') return { ...r, suppressed: 0 }
  const kept = r.findings.filter(isKept)
  const suppressed = r.findings.length - kept.length
  // The lane's verdict is computed over ALL findings, so it cannot stand once we
  // filter. DIFF-CLEAN says "the change under review introduced no failure" --
  // deliberately NOT the same claim as COMPLIANT, which would assert the whole
  // file is clean. A DIFF-CLEAN file may still carry a surviving SERIOUS.
  const attributableFail = kept.some((f) => f.severity === 'FAIL' && f.attributable !== false)
  return { ...r, findings: kept, suppressed, verdict: attributableFail ? 'NON-COMPLIANT' : 'DIFF-CLEAN' }
})

const totals = results.reduce((acc, r) => {
  for (const fnd of r.findings) {
    if (fnd.bucket === 'FIX') acc.fix++
    else if (fnd.bucket === 'SERIOUS') acc.serious++
    else if (fnd.bucket === 'IMPROVE') acc.improve++
    else if (fnd.bucket === 'SILENT') acc.silent++
    else if (fnd.bucket === 'SPECIAL') acc.special++
    // Guard on attributability, not just severity. Non-attributable SERIOUS
    // findings survive isKept by design, and a SERIOUS can carry FAIL.
    // Counting those here would print "N attributable FAIL" next to a
    // DIFF-CLEAN verdict that correctly ignored them.
    if (fnd.severity === 'FAIL' && fnd.attributable !== false) acc.fail++
  }
  acc.suppressed += r.suppressed || 0
  if (r.verdict === 'NON-COMPLIANT') acc.nonCompliant++
  if (r.verdict === 'DIFF-CLEAN') acc.diffClean++
  // Counted separately and never folded into diffClean -- a declined file is
  // neither a pass nor a failure, and a gate must be able to tell it apart.
  if (r.verdict === 'NOT-AUDITED') acc.notAudited++
  return acc
}, { fix: 0, serious: 0, improve: 0, silent: 0, special: 0, fail: 0, nonCompliant: 0, diffClean: 0, notAudited: 0, suppressed: 0 })

// The declined count is stated OUTSIDE the pass/fail tallies and never omitted
// when non-zero -- "N NOT-AUDITED" is the line that stops a reader inferring
// a clean gate from a report that never read the file.
const declined = totals.notAudited ? `, ${totals.notAudited} NOT-AUDITED (declined as out of scope — re-run under the lane named in each file's routing finding)` : ''

log(review
  ? `Reviewed ${results.length}/${input.files.length} files — ${totals.diffClean} DIFF-CLEAN, ${totals.nonCompliant} NON-COMPLIANT${declined}, ${totals.fail} attributable FAIL; dispositions SERIOUS=${totals.serious} FIX=${totals.fix} IMPROVE=${totals.improve} (${totals.suppressed} pre-existing finding(s) suppressed as not caused by this change; SILENT=${totals.silent} omitted)`
  : `Audited ${results.length}/${input.files.length} files — ${totals.nonCompliant} NON-COMPLIANT${declined}, ${totals.fail} FAIL findings; dispositions SERIOUS=${totals.serious} FIX=${totals.fix} IMPROVE=${totals.improve} (SILENT=${totals.silent} omitted)`)

return { perFile: results, totals, review }
