// md-domain audit_project_doc lane — DETECT workflow (before-Q&A phase).
//
// Fan-out detection + classification, one lane per target project document. Each
// lane reads the doc, loads the SINGLE self-contained audit-criteria doc, applies
// the cohesion-principles project_reference_md role criteria using the mechanical
// signals discover_project_doc.py already computed (orphan = inbound_citations, size =
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
// Invoked by the md-domain SKILL.md (audit_project_doc lane) only when auditing 2+ files (the
// multi-file threshold that equalizes the Workflow tool's per-run overhead).
// Single-file audits normally run inline in the main loop -- EXCEPT in review
// mode, where the threshold drops to 1 so every review-mode detect goes through
// a lane. Review mode exists to gate a submit/publish, so it cannot inherit the
// session model off the main loop; the lane is what pins model+effort and
// enforces the schema. See args.review below.
//
// args = {
//   files: [ { path: string, kind?: string, lines?: integer,
//              approx_tokens?: integer, inbound_citations?: integer,
//              cited_by?: string[] } ],
//     (kind/lines/approx_tokens/inbound_citations/cited_by come from discover_project_doc.py
//     on the own-skill path; they are OPTIONAL because a code-review subject-lens
//     caller passes only path/preImagePath/ancestorClaudeMdPaths. The lane
//     degrades gracefully when a discover_project_doc.py signal is absent -- it counts the
//     body itself and skips the orphan check, which needs the citer scan.)
//   review: boolean  (REVIEW MODE. When true, each finding is additionally
//            marked `attributable` -- whether the change under review caused it
//            -- via a targeted per-finding check against the pre-image. Lanes
//            stay mode-agnostic otherwise: they do NOT filter and do NOT change
//            the verdict. The caller filters on `attributable` and relabels the
//            verdict DIFF-CLEAN. See files[i].preImagePath.)
//   files[i].preImagePath: string|null  (review mode only. Absolute path to a
//            materialized copy of the doc as it was BEFORE the change under
//            review. The CALLER materializes it -- `p4 print //path#have`, or
//            `git show <base>:<path>` -- because this plugin is VCS-agnostic and
//            must not learn Perforce or git. null means the file is an ADD with
//            no pre-image, in which case every finding is attributable.)
//   files[i].ancestorClaudeMdPaths: string[]|undefined  (PD-11 ancestor-convention
//            check. The FULL ancestor CLAUDE.md chain above the subject on the
//            directory path to the workspace root, nearest-ancestor first,
//            EXCLUDING the subject. When present and non-empty the lane reads these
//            files, extracts EXPLICITLY declared conventions, and checks the
//            subject against them under criterion PD-11 (group Hygiene, taxonomy
//            S_ancestor_convention_violation). When absent/empty NO PD-11 finding
//            is emitted.)
//   refs:  { criteria: <abs path to references/standards/project-doc-standards.md>,
//            pluginRoot: <abs path to plugins/skills-kit> }
// }

export const meta = {
  name: 'md-domain-project-doc-detect',
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
            enum: ['A_misclassified_skill_ref', 'B_graduate_to_skill', 'C_fold_into_claude_md', 'D_move_into_existing_skill', 'E_crp_split', 'F_chained_reference', 'G_claude_md_back_reference', 'H_orphan', 'I_duplicates_skill', 'J_size_signal', 'K_unclassified', 'L_readme_stranded_fact', 'M_generated_missing_provenance', 'N_broken_link_identified_target', 'O_non_ascii_lookalike', 'P_foreign_absolute_path', 'Q_line_drift', 'R_stale_anchor', 'S_ancestor_convention_violation', 'N_user_standard_violation', 'none'],
            description: 'canonical suffixed taxonomy id (see the taxonomy table in references/standards/project-doc-standards.md); "none" for PASS/INFO/JUDGMENT that need no remediation',
          },
          bucket: { type: 'string', enum: ['FIX', 'SERIOUS', 'IMPROVE', 'SILENT', 'SPECIAL', 'NONE'], description: 'per-finding disposition assigned instance-level by the classifier (step 11)' },
          remediation: { type: 'string', description: 'concrete proposed remediation for FIX/SERIOUS/IMPROVE/SPECIAL; empty for SILENT/NONE' },
          attributable: { type: 'boolean', description: 'review mode: did the change under review cause this finding? Judged against the pre-image (step 10.5). ALWAYS true outside review mode -- nothing is being diffed, so every finding counts.' },
        },
        required: ['group', 'severity', 'criterion', 'message', 'line', 'taxonomy', 'bucket', 'remediation', 'attributable'],
      },
    },
    verdict: { type: 'string', enum: ['COMPLIANT', 'NON-COMPLIANT', 'NOT-AUDITED'], description: 'NOT-AUDITED = the criteria were never applied because the file is outside this audit\'s scope (PD-1 decline). It is NOT a passing verdict and must never be reported as one.' },
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
  throw new Error('project-doc-detect.js requires args.files = [{path, kind, lines, approx_tokens, inbound_citations, cited_by}]')
}
const refs = input.refs || {}
const review = input.review === true

function lanePrompt(f) {
  // The decline instruction must be reachable BOTH when the caller classified
  // the file (own-skill path: discover_project_doc.py ran) AND when no kind was supplied
  // (review-mode subject-lens call). Asserting "genuine project document" on a
  // kind-less call re-opens the fake gate for a misrouted claim: the lane gets
  // told to audit a file its criteria exclude, and DIFF-CLEAN comes back for a
  // file nobody meaningfully read.
  const declineInstruction = `Emit exactly ONE finding: group "Placement", criterion "placement_not_in_skill_dir", severity INFO, taxonomy "A_misclassified_skill_ref", bucket IMPROVE, message naming the correct auditor (skill_reference -> /md-domain audit skill via its owning SKILL.md; other_claude_artifact -> /md-domain audit skill or /md-domain audit claude-md) and stating plainly that THIS FILE WAS NOT AUDITED, with a \`remediation\` naming the auditor to re-run it under. Verdict NOT-AUDITED. Do NOT apply the other criteria. The finding is deliberately NOT SILENT: it is the caller's only signal that nothing read this file, and suppressing it next to a passing verdict is what makes a declined file read as an audited one.`
  const routingClause = f.kind && f.kind !== 'project_doc'
    ? `NOTE: discover_project_doc.py classified this target as \`${f.kind}\`, NOT a project document. ${declineInstruction}`
    : f.kind === 'project_doc'
      ? `This is a genuine project document — apply all the criteria below.`
      : `No discover_project_doc.py \`kind\` signal was provided (typical for a review-mode subject-lens call, where the caller does not classify). Run the PD-1 routing test YOURSELF from the path shape FIRST: a file inside a \`*/skills/*/references/\` directory is a \`skill_reference\`, and a \`CLAUDE.md\` / \`SKILL.md\` basename is an \`other_claude_artifact\` -- if either matches: ${declineInstruction} Otherwise treat it as a genuine project document and apply all the criteria below.`

  const orphanClause = f.inbound_citations === 0
    ? `discover_project_doc.py reports ZERO inbound citations — this doc is an ORPHAN in the agent load graph. Raise PD-4 (group ADP, criterion adp_discoverability, severity JUDGMENT, taxonomy H_orphan). Disposition IMPROVE by default (orphan-linking is a structural judgment -- offer add-a-CLAUDE.md-pointer or retire as a one-line pitch); but an intentionally human-only / historical-record / companion-source / agent-definition doc is an accepted structural pattern -> SILENT (not surfaced). Do NOT auto-FAIL — a doc can legitimately serve human readers who open it directly.`
    : typeof f.inbound_citations === 'number'
      ? `discover_project_doc.py reports ${f.inbound_citations} inbound citation(s); the doc is reachable in the load graph. PD-4 PASSes (no orphan finding) unless you see a more specific discoverability problem.`
      : `No inbound-citation signal was provided (e.g. a review-mode subject-lens call, where the discover_project_doc.py citer scan does not run). Orphan status is UNKNOWABLE without that scan, so PD-4 is N/A here -- do NOT emit an H_orphan finding.`

  const hasSize = typeof f.lines === 'number' || typeof f.approx_tokens === 'number'
  const sizeClause = !hasSize
    ? `Size signals were not precomputed (no discover_project_doc.py record). Count the body yourself in step 1 and run the PD-3 unitary-reading-task evaluation ONLY if the body is clearly over ~500 lines / ~3000 tokens AND its sections fire on genuinely different sub-triggers -> JUDGMENT E_crp_split (group CRP). Size alone is NEVER a FAIL.`
    : (f.lines > 500 || f.approx_tokens > 3000)
      ? `Size signal: ${f.lines} effective lines / ~${f.approx_tokens} tokens is OVER the threshold. Run the PD-3 unitary-reading-task evaluation: do the sections fire on genuinely different sub-triggers? If yes -> JUDGMENT E_crp_split (group CRP); if no -> INFO J_size_signal only (a large single-task doc is correct). Size alone is NEVER a FAIL.`
      : `Size (${f.lines} lines / ~${f.approx_tokens} tokens) is under the threshold; raise a CRP split finding only if the sections obviously serve different reading tasks regardless of size.`

  const reviewClause = !review
    ? `Not review mode. Set \`attributable: true\` on EVERY finding -- nothing is being diffed, so every finding counts. Do not read any pre-image.`
    : f.preImagePath
      ? `REVIEW MODE. This audit gates a submit, so it must report only what the change under review actually caused. For EACH non-PASS finding you produced above, run a TARGETED check against the pre-image at ${f.preImagePath} (the doc as it was BEFORE the change): does this same criterion fire at this same anchor in the pre-image?
   - Fires in the pre-image too -> \`attributable: false\` (pre-existing; not this change's doing).
   - Does not fire in the pre-image -> \`attributable: true\`.
   Do NOT re-run the whole audit on the pre-image. Ask one narrow factual question per finding; that is cheaper and far more stable than differencing two full reports.
   Match on (criterion, taxonomy, normalized anchor) -- NEVER on line number or message wording. Line numbers shift and phrasing varies; a finding that moved or got reworded is the SAME finding. When a pre-image finding plausibly corresponds to this one, be GENEROUS and call it non-attributable: a false "pre-existing" is a missed nag, a false "attributable" is an accusation the author cannot act on.
   PASS / INFO / NONE findings are ALWAYS \`attributable: true\` -- they carry no remediation, so "did the change cause it" is not a meaningful question and a \`false\` there would silently delete the row from the report.
   If the pre-image cannot be read (missing, empty, unreadable), do NOT guess: set \`attributable: true\` on every finding and add one JUDGMENT finding, group Hygiene, taxonomy "none", bucket "NONE", message "pre-image unreadable -- findings are unfiltered". Over-reporting is the safe direction; silently suppressing is not.
   Attributable means CAUSED BY, not LOCATED IN. A finding anchored far from the edited lines is still attributable if the change created it. Judge causation, not proximity.
   You still report EVERY finding with its real disposition. Do NOT drop, downgrade, or re-bucket anything based on attributability -- the caller filters.`
      : `REVIEW MODE, and this file has NO pre-image (it is an ADD introduced by the change under review). Every finding is therefore caused by this change: set \`attributable: true\` on ALL of them. Do not look for a pre-image.`

  const ancestorPaths = Array.isArray(f.ancestorClaudeMdPaths) ? f.ancestorClaudeMdPaths : []
  const ancestorConventionsClause = ancestorPaths.length > 0
    ? `PD-11 ANCESTOR-DECLARED CONVENTIONS. Read each ancestor CLAUDE.md, nearest-ancestor first: ${ancestorPaths.map((p) => `"${p}"`).join(', ')}. These load ambient in any session that touches this doc, so a convention they EXPLICITLY declare (ASCII-only mandates, "no absolute paths in shared files", stated formatting/structure rules) binds the doc too. For each such convention, check whether the doc VIOLATES it.
   Rule-extraction posture (mirror the code-review reviewer_a): flag a violation ONLY when you can quote the exact declared rule VERBATIM from an ancestor. No inferred conventions, no generic best-practice, no "spirit of" a rule, no convention you believe is standard but the ancestor did not write down. If you cannot quote the ancestor's rule text verbatim, do NOT raise the finding.
   Emit each violation as group "Hygiene", taxonomy S_ancestor_convention_violation, severity FAIL, anchored on the SUBJECT line that violates the rule. The \`message\` MUST carry (a) the verbatim ancestor rule quote and (b) the source path of the ancestor CLAUDE.md that declared it. Disposition is assigned in step 10 like any other convention-violation fix -- normally FIX (a mechanical correction against a documented project convention), SERIOUS when the violation reveals a real-world problem the rule exists to prevent (e.g. a committed secret an ancestor forbids).
   EXCEPTION AWARENESS: a convention an ancestor declares may itself carry an EXPLICIT scoped exception (e.g. "ASCII only, EXCEPT developer names in the contributors section may contain non-ASCII characters"). When an ancestor's exception COVERS this exact instance -- right file scope AND the right content kind -- that instance is NOT a violation: do NOT emit the S_ancestor_convention_violation finding for it. The exception must be written down and actually cover this instance (same verbatim-quote posture -- no inferred or stretched exceptions; when in doubt the rule still binds and the finding fires). PRECEDENCE: the SAME declared rule + exception also governs the step-10 built-in universal-convention FIX (non-ASCII / hardcoded-path); an exception that silences THIS PD-11 finding silences that built-in FIX too, and vice versa -- the two must never contradict on a given instance.`
    : `No ancestor CLAUDE.md paths were supplied; do NOT run the PD-11 ancestor-convention check and emit no S_ancestor_convention_violation findings.`

  const builtinConventionExceptionClause = ancestorPaths.length > 0
    ? ` ANCESTOR-DECLARED EXCEPTION CARVE-OUT: the ancestor CLAUDE.md files supplied for step 9c may EXPLICITLY declare a SCOPED EXCEPTION to one of these universal conventions -- e.g. "ASCII only, EXCEPT developer names in the contributors section may contain non-ASCII characters". Before emitting a convention-violation FIX for a non-ASCII look-alike (O) or a hardcoded absolute / foreign-machine path (P), check those ancestors for an explicit exception that COVERS this exact instance -- the right file scope AND the right content kind. If one does, do NOT emit the FIX: demote the finding to PASS (taxonomy "none", bucket "NONE") -- or INFO if it is worth noting -- and put the verbatim quoted exception rule plus the ancestor source path in its \`message\`. Same verbatim-quote posture as PD-11: the exception must be written down and actually cover this instance; no inferred, generic, or stretched exceptions, and when in doubt the built-in check STILL fires. PRECEDENCE: this carve-out and PD-11 (step 9c) read the SAME ancestor declarations, so a declared rule + its exception must yield ONE consistent outcome for a given instance -- an exception that silences the PD-11 S_ancestor_convention_violation finding silences this built-in convention FIX too, and vice versa; they must never contradict (PD-11 silent while the built-in FIX still fires is exactly the bug this carve-out removes).`
    : ``

  const standardsPaths = Array.isArray(f.standardsPaths) ? f.standardsPaths : []
  const standardsClause = standardsPaths.length > 0
    ? `USER-AUTHORED STANDARDS. Read each standards file, nearest-layer first: ${standardsPaths.map((p) => `"${p}"`).join(', ')}. Each is a *-standards.md carrying a fenced \`standards_set:\` block whose \`criteria[]\` are the project's or user's own opinions for this artifact type. Apply ONLY criteria whose \`statement\` you can quote VERBATIM from the standards file -- same rule-extraction posture as the ancestor-convention check: no inferred rules, no generic best-practice, no "spirit of" a criterion; if you cannot quote the statement verbatim, do NOT raise the finding. SKIP any criterion whose \`enforcement\` is \`mechanical\` -- those are the audit.py validator's job (it runs them under --config), not yours; you evaluate only judgment criteria (enforcement \`judgment\` or absent). For each violated criterion emit group "Hygiene", taxonomy N_user_standard_violation, severity taken from the criterion's declared \`severity\` (fail -> FAIL, info -> INFO, judgment -> JUDGMENT), anchored on the doc line that violates it. The \`message\` MUST carry (a) the verbatim criterion statement, (b) the criterion \`id\`, and (c) the source standards-file path. Disposition is assigned in step 10 from the severity: a fail-severity violation is SERIOUS (a hard user-declared rule the auditor cannot mechanically satisfy -- surface at the top, never auto-fix), an info-severity note is IMPROVE (one-line pitch), a judgment-severity call is JUDGMENT (surfaced for review).`
    : `No user-authored standards files were supplied; do NOT apply any user standards and emit no N_user_standard_violation findings.`

  const disabledCriteria = Array.isArray(input.disabledCriteria) ? input.disabledCriteria : []
  const disabledClause = disabledCriteria.length > 0
    ? `DISABLED CRITERIA. The run configuration switched these optional criterion/rule ids OFF: ${disabledCriteria.map((d) => `"${d}"`).join(', ')}. SUPPRESS any finding whose criterion id or rule id matches one in that list -- do not emit it and do not count it toward the verdict. That list only ever names OPTIONAL ids; architectural (schema/contract) and integrity (frontmatter, reachability, convention) checks are NEVER in it, so never suppress one of those on account of this list.`
    : `No criteria were disabled for this run; apply every criterion normally.`

  return `You are ONE lane of a project-document audit. Audit exactly one file and return structured findings. This is DETECTION ONLY — do not modify any file.

Target: ${f.path}
Kind:   ${f.kind || 'project_doc'}
Signals: ${typeof f.lines === 'number' ? `${f.lines} lines` : 'lines n/a'}, ${typeof f.approx_tokens === 'number' ? `~${f.approx_tokens} tokens` : 'tokens n/a'}, ${typeof f.inbound_citations === 'number' ? `${f.inbound_citations} inbound citations` : 'inbound n/a'}${Array.isArray(f.cited_by) && f.cited_by.length ? ` (e.g. ${f.cited_by.slice(0, 3).join(', ')})` : ''}

${routingClause}

Steps:
1. Read the target file.
2. Read the project-doc standards at ${refs.criteria}. This file is self-contained: every testable rule is stated with the cohesion principle it derives from. Do NOT load any other framework document. (Recap: project references are the escape-hatch / nursery for STILL-EMERGING content. When content stabilizes, it moves to its TRIGGER-APPROPRIATE mature home — a skill is NOT the default home for all reference content. The criteria check whether the content has matured past a standalone doc, serves one reading task, is reachable, and does not duplicate a skill.)
3. PLACEMENT (PD-2 maturation): judge whether the content has stabilized past the project-doc/nursery stage, and if so route it by its NATURAL TRIGGER SHAPE (cohesion-principles placement_follows_trigger_shape). Ask "what is the trigger for needing this?": a TASK-shaped trigger is a VERB the session performs (authoring an X, running an evaluator) needed wherever the activity happens -> graduate to a skill (taxonomy B); a LOCATION-shaped trigger is WORKING UNDER A DIRECTORY (a config subtree, source dirs, a package) -> fold into / reference from that directory's CLAUDE.md (taxonomy C), the PREFERRED home for directory-scoped knowledge because a CLAUDE.md auto-loads when any file beneath it is touched, at zero session-wide context cost; when an EXISTING skill already owns the (task-shaped) topic, move into that skill's references/ (taxonomy D). Knowledge with BOTH shapes: prefer the location home (C) and let a skill point at it. Recommend skill-graduation (B) ONLY for task-shaped knowledge — do NOT pitch a skill for location-scoped knowledge. If the content is genuinely emerging/unstructured, NONE fires and PD-2 PASSes. All maturation findings are JUDGMENT/IMPROVE — never FAIL a doc for not yet being a skill.
4. CRP (PD-3 + size): ${sizeClause}
5. ADP discoverability (PD-4): ${orphanClause}
6. ADP one-hop (PD-5): scan outbound doc-to-doc citations; a required reading CHAIN (A -> B -> C) is a FAIL (group ADP, taxonomy F_chained_reference). A single informational pointer to a sibling doc or SKILL.md is fine.
7. ADP back-reference (PD-6): if the body cites specific CLAUDE.md SECTION CONTENT as required reading (reversing load order), that is a FAIL (group ADP, taxonomy G_claude_md_back_reference). A pure orientation mention ("see the root CLAUDE.md") is permitted.
8. CCP (PD-8): if a skill already owns this doc's topic and the doc restates (rather than points at) that skill's content, that is a FAIL (group CCP, taxonomy I_duplicates_skill); INFO if the doc predates the skill and graduation is in progress.
9. Hygiene (PD-H1): outbound file-path links must resolve. A broken file-path reference is a FAIL (group Hygiene). Do NOT check /skill-name or skill:"..." link integrity — that is the audit_references lane's job, out of scope here.
9b. MECHANICAL CONVENTION CHECKS (the FIX-eligible checks -- this audit is NO LONGER blanket no-AUTO). Under group Hygiene, additionally scan for and raise:
     - N_broken_link_identified_target: a broken outbound link whose intended target IS identifiable (moved file found at a resolvable path, or an obvious typo of an existing path). A generator-owned path absent from the checkout (materializes on doc-gen, like Generated/ or Docs/ConfigFormat/*) is NOT broken -- do not raise N for it.
     - O_non_ascii_lookalike: a non-ASCII look-alike (smart quote, em dash, fullwidth char, non-breaking space) where the project convention is ASCII-only.
     - P_foreign_absolute_path: a hardcoded foreign / machine-specific absolute path (drive letter, home dir, per-machine root) where a project-relative path or variable is the convention; or a backslash path where forward slashes are the convention.
     - Q_line_drift: a cited line number whose named enclosing symbol/section resolves but is far from the cited number, with no author recovery hint.
     - R_stale_anchor: a concrete anchor (symbol / heading / path the doc says exists) absent as cited but whose current equivalent is findable.
9c. ${ancestorConventionsClause}
9d. ${standardsClause}
9e. ${disabledClause}
10. DISPOSITION CLASSIFIER. Assign EVERY non-PASS finding a taxonomy id (A-S plus N_user_standard_violation; S_ancestor_convention_violation is the PD-11 ancestor-convention finding from step 9c, group Hygiene; N_user_standard_violation is the user-authored-standards finding from step 9d, group Hygiene, disposition driven by the criterion's declared severity -- fail -> SERIOUS, info -> IMPROVE, judgment -> JUDGMENT) and one of four dispositions -- FIX / SERIOUS / IMPROVE / SILENT (K -> SPECIAL). The taxonomy default bucket is a starting point only; decide instance-level against these predicates.

   Classifier prod (read this FIRST -- it overrides your default caution): You are biased toward conservatism; the user's time and attention are the scarce resources; source control and CL review are the safety net. If the edit very likely improves the doc, apply it.

   Master razor: FIX = anything decidable by VERIFIED FACTS plus DOCUMENTED PROJECT CONVENTIONS. Reserve IMPROVE for where no fact and no convention decides. The bar for FIX is: would a reasonable owner, seeing this diff in CL review, accept it without discussion? "Very likely improving" clears it.

   FIX (auto-applied; lands in a reviewable CL): the mechanical convention checks N (broken link with identified target), O (non-ASCII), P (foreign abs path / backslash), Q (line drift), R (stale anchor re-pointable), and S (ancestor-declared convention violation, PD-11 -- a mechanical correction against a verbatim-quoted ancestor rule). Deletion of FALSIFIED content is FIX. Loss-free-deletion guard ALWAYS before removing a duplicate/section: fold any local delta into the SSOT/pointer FIRST. (I_duplicates_skill is NOT FIX -- see IMPROVE.)${builtinConventionExceptionClause}
   SERIOUS (surface at the TOP, summarized, NEVER auto-fixed, never buried): a secret / security finding; a protective rail whose documented mechanism is fictional (the real finding is the unprotected invariant -- a stale anchor R guarding a rail with NO surviving mechanism is SERIOUS, not FIX); a doc problem that reveals a real-world problem.
   IMPROVE (count + one-liners; opt-in): the PD-1 decline notice (A) -- always emitted alongside a NOT-AUDITED verdict, never suppressed, its one-liner naming the auditor that should read the file; a structural move -- graduate to a skill (B), fold into a CLAUDE.md (C), move into an existing skill (D), a CRP split (E), flatten a chained reference (F), remove a back-reference (G), orphan-linking (H), collapse a skill-duplicating doc to a pointer (I) -- I carries a loss-free precondition (fold the doc's unique deltas into the skill BEFORE removal) that no auto-apply pass can satisfy, so it is opt-in, not FIX; README stranded-fact re-home (L), add a generation record (M); or a trim of TRUE content passing the one-line test.
   SILENT (do NOT surface; no hedging): a do-nothing conclusion ("accept as-is -> PASS"); a validator detection artifact; an accepted structural pattern (an agent-definition file with zero inbound citations, a historical record, a companion-source PDF, an intentionally human-only orphan).
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
10.5. ATTRIBUTABILITY. ${reviewClause}
11. Verdict: NOT-AUDITED if the PD-1 routing decline fired (the target is not a project document, so the criteria were never applied) — never COMPLIANT, which would assert a clean file nobody read. Otherwise NON-COMPLIANT if ANY finding has severity FAIL; otherwise COMPLIANT. INFO/JUDGMENT never gate (maturation, orphan, and split-candidacy are all JUDGMENT — a useful-where-it-sits doc is COMPLIANT). Disposition is orthogonal to the verdict.

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
// coverage from a clean run.
const declined = totals.notAudited ? `, ${totals.notAudited} NOT-AUDITED (declined as out of scope — re-run under the auditor named in each file's routing finding)` : ''

log(review
  ? `Reviewed ${results.length}/${input.files.length} project docs — ${totals.diffClean} DIFF-CLEAN, ${totals.nonCompliant} NON-COMPLIANT${declined}, ${totals.fail} attributable FAIL; dispositions SERIOUS=${totals.serious} FIX=${totals.fix} IMPROVE=${totals.improve} (${totals.suppressed} pre-existing finding(s) suppressed as not caused by this change; SILENT=${totals.silent} omitted)`
  : `Audited ${results.length}/${input.files.length} project docs — ${totals.nonCompliant} NON-COMPLIANT${declined}, ${totals.fail} FAIL findings; dispositions SERIOUS=${totals.serious} FIX=${totals.fix} IMPROVE=${totals.improve} (SILENT=${totals.silent} omitted)`)

return { perFile: results, totals, review }
