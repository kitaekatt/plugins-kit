// md-domain audit_skill lane — DETECT workflow (before-Q&A phase).
//
// Fan-out detection + classification, one lane per target file. The lane audits
// the `skill` artifact's TWO subject shapes: a SKILL.md (the contract root) and
// a skill REFERENCE DOCUMENT (`*/skills/<name>/references/*.md`, an L3 member).
// The subject is decided from the path; each shape gets its own criteria set --
// skill-standards.md sections 1-9 for a SKILL.md, section 10 for a reference.
// For a SKILL.md the lane runs the mechanical validator (skills_kit_lib.audit)
// for the Schema
// group, applies the CCP / CRP / ADP / decision-provenance judgment from the
// recap embedded in the lane prompt, and classifies every finding into the
// A-N taxonomy; for a reference document it runs no validator and classifies
// into the O-R taxonomy (plus the shared H / M / N / K ids). Both then take
// one of four dispositions (FIX / SERIOUS / IMPROVE / SILENT;
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
// Invoked by the md-domain SKILL.md (audit_skill lane) when auditing 2+ files (the multi-file
// threshold that equalizes the Workflow tool's per-run overhead). Single-file
// audits normally run inline in the main loop -- EXCEPT in review mode, where the
// threshold drops to 1 so every review-mode detect goes through a lane. Review
// mode exists to gate a submit/publish, so it cannot inherit the session model off
// the main loop; the lane is what pins model+effort and enforces the schema. See
// args.review below.
//
// args = {
//   files: [ { path: string, skillType?: string, kind?: string } ],
//     (kind is the caller's artifact classification. "skill" -> apply the SKILL.md
//     criteria; "skill_reference" -> apply the reference-document criteria
//     (skill-standards.md section 10); any OTHER explicit value -> decline with
//     NOT-AUDITED; ABSENT -> the lane self-applies the shape test -- basename
//     SKILL.md, or a path inside a */skills/*/references/ folder -- and declines
//     on a non-match. See the decline contract in
//     references/lanes/audit-lane.md step 2a.)
//   review: boolean  (REVIEW MODE. When true, each finding is additionally
//            marked `attributable` -- whether the change under review caused it
//            -- via a targeted per-finding check against the pre-image. Lanes
//            stay mode-agnostic otherwise: they do NOT filter and do NOT change
//            the verdict. The caller filters on `attributable` and relabels the
//            verdict DIFF-CLEAN. See files[i].preImagePath.)
//   files[i].preImagePath: string|null  (review mode only. Absolute path to a
//            materialized copy of the target document as it was BEFORE the change under
//            review. The CALLER materializes it -- `p4 print //path#have`, or
//            `git show <base>:<path>` -- because this plugin is VCS-agnostic and
//            must not learn Perforce or git. null means the file is an ADD with
//            no pre-image, in which case every finding is attributable.)
//   files[i].ancestorClaudeMdPaths: string[]|undefined  (H-11 ancestor-convention
//            check. The FULL ancestor CLAUDE.md chain above the target on the
//            directory path to the workspace root, nearest-ancestor first,
//            EXCLUDING the subject. Includes the skill's co-located CLAUDE.md when
//            one exists. When present and non-empty the lane reads these files,
//            extracts EXPLICITLY declared conventions, and checks the target
//            against them under criterion H-11 (group Hygiene, taxonomy
//            M_ancestor_convention_violation). When absent/empty NO H-11 finding
//            is emitted.)
//   refs:  { pluginRoot: <abs path to plugins/skills-kit (parent of skills_kit_lib)>,
//            venvPython: <abs path to skills-kit venv python> }
// }
// The mechanical validator is invoked as a module:
//   (cd <pluginRoot> && <venvPython> -m skills_kit_lib.audit <file> --json --config)
//   --config makes audit.py honor the resolved standards config (drop disabled
//   mechanical rows, overlay thresholds); disabled ids also arrive as
//   args.disabledCriteria and standards files per-file as files[i].standardsPaths.

export const meta = {
  name: 'md-domain-skill-detect',
  description: 'Fan-out skill-artifact audit (a SKILL.md contract, or a skill reference document prose): apply the subject criteria set + classify, one lane per file (detection only, no edits)',
  phases: [{ title: 'Audit', detail: 'one lane per SKILL.md or skill-reference file' }],
}

const FILE_FINDINGS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    path: { type: 'string' },
    skill_name: { type: 'string', description: 'the owning skill name (for a reference-document subject, the skill whose references/ folder contains it)' },
    skill_type: { type: 'string', description: 'the declared skill-type; "(skill reference)" when the subject is a references/*.md rather than a SKILL.md' },
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
          taxonomy: {
            type: 'string',
            enum: ['A_missing_required_frontmatter', 'B_description_quality', 'C_wrong_skill_type', 'D_mixed_type_signal', 'E_schema_validation_failure', 'F_ccp_misallocation', 'G_crp_violation', 'H_adp_back_reference', 'I_decision_provenance', 'J_hygiene_threshold', 'K_unclassified', 'L_load_graph_gap', 'M_ancestor_convention_violation', 'N_user_standard_violation', 'O_broken_inbound_anchor', 'P_internal_contradiction', 'Q_overstated_claim', 'R_maintainer_only_material', 'none'],
            description: 'canonical suffixed taxonomy id (A..N = the SKILL.md subject, section 7.2 of references/standards/skill-standards.md; O..R = the skill-reference subject, section 10.4); "none" for PASS/INFO/JUDGMENT that need no remediation, and for the artifact-shape decline finding',
          },
          bucket: { type: 'string', enum: ['FIX', 'SERIOUS', 'IMPROVE', 'SILENT', 'SPECIAL', 'NONE'], description: 'per-finding disposition assigned instance-level by the classifier (step 6)' },
          remediation: { type: 'string', description: 'concrete proposed remediation for FIX/SERIOUS/IMPROVE/SPECIAL; empty for SILENT/NONE' },
          attributable: { type: 'boolean', description: 'review mode: did the change under review cause this finding? Judged against the pre-image (step 6.5). ALWAYS true outside review mode -- nothing is being diffed, so every finding counts.' },
        },
        required: ['group', 'severity', 'criterion', 'message', 'line', 'taxonomy', 'bucket', 'remediation', 'attributable'],
      },
    },
    verdict: { type: 'string', enum: ['COMPLIANT', 'NON-COMPLIANT', 'NOT-AUDITED'], description: 'NOT-AUDITED = the criteria were never applied because the file is neither a SKILL.md nor a skill reference document (the artifact-shape decline). It is NOT a passing verdict and must never be reported as one.' },
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
  throw new Error('skill-detect.js requires args.files = [{path}]')
}
const refs = input.refs || {}
const review = input.review === true

// The `skill` artifact's two subject shapes, decided from the PATH. `kind` (when
// the caller supplies one) decides whether the file is DECLINED; the path shape
// decides WHICH criteria set applies, so a kind-less review-mode call selects the
// right set without the model having to infer it.
const isSkillMdPath = (p) => /(^|[\\/])SKILL\.md$/i.test(p)
const isSkillRefPath = (p) => /(^|[\\/])skills[\\/][^\\/]+[\\/]references[\\/].+\.md$/i.test(p)
const subjectOf = (f) => {
  if (f.kind === 'skill') return 'skill'
  if (f.kind === 'skill_reference') return 'skill_reference'
  return isSkillMdPath(f.path) ? 'skill' : isSkillRefPath(f.path) ? 'skill_reference' : 'skill'
}

function lanePrompt(f) {
  const subject = subjectOf(f)
  const isRef = subject === 'skill_reference'
  // The decline contract (audit-lane.md step 2a), generalized from PD-1. The
  // decline instruction must be reachable BOTH when the caller classified the
  // file (own-lane path: discover_skill.py ran) AND when no kind was supplied
  // (review-mode subject-lens call). Asserting "genuine SKILL.md" on a kind-less
  // call re-opens the fake gate: the lane gets told to audit a file its criteria
  // exclude, and DIFF-CLEAN comes back for a file nobody meaningfully read.
  const declineInstruction = `Emit exactly ONE finding: group "Schema", criterion "artifact_shape_not_skill_md", severity INFO, taxonomy "none", bucket IMPROVE (this is the one INFO finding that is NOT bucket NONE -- it is a routing conclusion carrying a remediation, not a finding against the file), message naming the correct lane (a CLAUDE.md / CLAUDE.local.md -> \`/md-domain audit claude-md\`; a standalone project document -> \`/md-domain audit project-doc\`) and stating plainly that THIS FILE WAS NOT AUDITED, with a \`remediation\` naming the lane to re-run it under. Verdict NOT-AUDITED. Do NOT apply the other criteria and do NOT edit anything. The finding is deliberately NOT SILENT: it is the caller's only signal that nothing read this file, and suppressing it next to a passing verdict is what makes a declined file read as an audited one. It is never suppressed in either mode; in review mode set \`attributable: true\` on it and skip the attributability check entirely.`
  const applyClause = isRef
    ? `Apply the SKILL-REFERENCE criteria below (skill-standards.md section 10) -- NOT the SKILL.md contract criteria. A reference document has no frontmatter, no typed YAML block and no schema.`
    : `Apply the SKILL.md criteria below (skill-standards.md sections 1-9).`
  const routingClause = f.kind && f.kind !== 'skill' && f.kind !== 'skill_reference'
    ? `NOTE: the caller classified this target as \`${f.kind}\`, which is neither a SKILL.md nor a skill reference document. ${declineInstruction}`
    : f.kind === 'skill'
      ? `This is a genuine SKILL.md — apply all the criteria below.`
      : f.kind === 'skill_reference'
        ? `The caller classified this target as a skill reference document. ${applyClause}`
        : `No \`kind\` signal was provided (typical for a review-mode subject-lens call, where the caller does not classify). Run the artifact-shape test YOURSELF FIRST: this lane audits the \`skill\` artifact's TWO subject shapes -- (a) a file whose BASENAME is \`SKILL.md\`, and (b) a skill REFERENCE DOCUMENT, i.e. a \`.md\` file inside a \`*/skills/<name>/references/\` folder. A \`CLAUDE.md\` / \`CLAUDE.local.md\` or any other standalone document is NEITHER -- if the path matches neither shape: ${declineInstruction} Otherwise apply the criteria for the shape it matched. ${applyClause}`

  const schemaClause = isRef
    ? `NOT APPLICABLE to a skill reference document. The mechanical validator's subject is a SKILL.md or a CLAUDE.md; there is no contract on a reference to validate. Do NOT run it, and do NOT emit a "validator unavailable" finding -- emit no Schema-group contract findings at all for this subject.`
    : refs.pluginRoot && refs.venvPython
    ? `Run the mechanical validator via Bash (it is a package module, so cd into the plugin root first):\n    (cd "${refs.pluginRoot}" && "${refs.venvPython}" -m skills_kit_lib.audit "${f.path}" --json --config)\n(--config makes audit.py drop disabled mechanical rows and overlay the resolved thresholds; the disabled ids also arrive as args.disabledCriteria, so honor both.) Map its rows into Schema-group findings: a universal-rule or YAML-schema FAIL is a Schema FAIL. Specifically: missing/malformed required frontmatter -> taxonomy A (default FIX -- add the mechanical default; authorial fields route to IMPROVE); description length/directive-form/exclusion-clause FAIL -> taxonomy B (IMPROVE, authorial); a YAML contract FAIL (missing required key, wrong type, list below min_len, forbidden key) -> taxonomy E (default FIX for a missing-default field; authorial or forbidden-key -> IMPROVE); a mixed-type signal (>1 canonical root, or the mixed-type heuristic) -> taxonomy D (IMPROVE, unless the orientation-summary exception applies, then JUDGMENT); a load-graph row (orphaned references/ file, unlinked member directory, two-hop-only reference, dangling index entry) -> taxonomy L (IMPROVE default; a dangling index path with an identified correct target is a mechanical FIX; an accepted internal-helper orphan is SILENT), group ADP, keeping the validator's severity (FAIL gates; JUDGMENT does not). Assign the final disposition in step 6, not here. If the validator is unavailable, emit one Schema finding severity JUDGMENT ("validator unavailable") and continue — never fail a file for that.`
    : `Validator path was not provided; emit one Schema finding severity JUDGMENT ("validator unavailable") and continue with cohesion judgment only.`

  const reviewClause = !review
    ? `Not review mode. Set \`attributable: true\` on EVERY finding -- nothing is being diffed, so every finding counts. Do not read any pre-image.`
    : f.preImagePath
      ? `REVIEW MODE. This audit gates a submit, so it must report only what the change under review actually caused. For EACH non-PASS finding you produced above, run a TARGETED check against the pre-image at ${f.preImagePath} (the target document as it was BEFORE the change): does this same criterion fire at this same anchor in the pre-image?
   - Fires in the pre-image too -> \`attributable: false\` (pre-existing; not this change's doing).
   - Does not fire in the pre-image -> \`attributable: true\`.
   Do NOT re-run the whole audit on the pre-image. Ask one narrow factual question per finding; that is cheaper and far more stable than differencing two full reports.
   Match on (criterion, taxonomy, normalized anchor) -- NEVER on line number or message wording. Line numbers shift and phrasing varies; a finding that moved or got reworded is the SAME finding. When a pre-image finding plausibly corresponds to this one, be GENEROUS and call it non-attributable: a false "pre-existing" is a missed nag, a false "attributable" is an accusation the author cannot act on.
   PASS / INFO / NONE findings are ALWAYS \`attributable: true\` -- they carry no remediation, so "did the change cause it" is not a meaningful question and a \`false\` there would silently delete the row from the report.
   If the pre-image cannot be read (missing, empty, unreadable), do NOT guess: set \`attributable: true\` on every finding and add one JUDGMENT finding, taxonomy "none", bucket "NONE", message "pre-image unreadable -- findings are unfiltered". Over-reporting is the safe direction; silently suppressing is not.
   Attributable means CAUSED BY, not LOCATED IN. A finding anchored far from the edited lines is still attributable if the change created it -- e.g. the change adds a section that pushes the body over the CRP threshold, and the G finding anchors on an older section. Judge causation, not proximity.
   You still report EVERY finding with its real disposition. Do NOT drop, downgrade, or re-bucket anything based on attributability -- the caller filters.`
      : `REVIEW MODE, and this file has NO pre-image (it is an ADD introduced by the change under review). Every finding is therefore caused by this change: set \`attributable: true\` on ALL of them. Do not look for a pre-image.`

  const ancestorPaths = Array.isArray(f.ancestorClaudeMdPaths) ? f.ancestorClaudeMdPaths : []
  const ancestorConventionsClause = ancestorPaths.length > 0
    ? `H-11 ANCESTOR-DECLARED CONVENTIONS. Read each ancestor CLAUDE.md, nearest-ancestor first: ${ancestorPaths.map((p) => `"${p}"`).join(', ')}. These load ambient in any session that touches the target document, so a convention they EXPLICITLY declare (ASCII-only mandates, "no absolute paths in shared files", temporal-deixis bans, stated formatting/structure rules) binds the target too -- a skill reference document exactly as much as a SKILL.md. For each such convention, check whether the TARGET VIOLATES it.
   Rule-extraction posture (mirror the code-review reviewer_a): flag a violation ONLY when you can quote the exact declared rule VERBATIM from an ancestor. No inferred conventions, no generic best-practice, no "spirit of" a rule, no convention you believe is standard but the ancestor did not write down. If you cannot quote the ancestor's rule text verbatim, do NOT raise the finding.
   Emit each violation as group "Hygiene", taxonomy M_ancestor_convention_violation, severity FAIL, anchored on the SUBJECT line that violates the rule. The \`message\` MUST carry (a) the verbatim ancestor rule quote and (b) the source path of the ancestor CLAUDE.md that declared it. Disposition is assigned in step 6 like any other convention-violation fix -- normally FIX (a mechanical correction against a documented project convention), SERIOUS when the violation reveals a real-world problem the rule exists to prevent (e.g. a committed secret an ancestor forbids).
   EXCEPTION AWARENESS: a convention an ancestor declares may itself carry an EXPLICIT scoped exception (e.g. "ASCII only, EXCEPT developer names in the contributors section may contain non-ASCII characters"). When an ancestor's exception COVERS the specific instance -- right file scope AND right content kind -- that instance is NOT a violation: do NOT emit the M_ancestor_convention_violation finding for it. The exception must be written down and actually cover this instance (same verbatim-quote posture -- no inferred or stretched exceptions; when in doubt the rule still binds and the finding fires). PRECEDENCE: the SAME declared rule + exception also governs the step-6 built-in universal-convention FIX (non-ASCII / hardcoded-path); an exception that silences THIS H-11 finding silences that built-in FIX too, and vice versa -- the two must never contradict on a given instance.`
    : `No ancestor CLAUDE.md paths were supplied; do NOT run the H-11 ancestor-convention check and emit no M_ancestor_convention_violation findings.`

  const builtinConventionExceptionClause = ancestorPaths.length > 0
    ? ` ANCESTOR-DECLARED EXCEPTION CARVE-OUT: the ancestor CLAUDE.md files supplied for step 3.5 may EXPLICITLY declare a SCOPED EXCEPTION to one of these universal conventions -- e.g. "ASCII only, EXCEPT developer names in the contributors section may contain non-ASCII characters". Before emitting a convention-violation FIX for a non-ASCII look-alike or a hardcoded absolute / foreign-machine path, check those ancestors for an explicit exception that COVERS this exact instance -- the right file scope AND the right content kind. If one does, do NOT emit the FIX: demote the finding to PASS (taxonomy "none", bucket "NONE") -- or INFO if it is worth noting -- and put the verbatim quoted exception rule plus the ancestor source path in its \`message\`. Same verbatim-quote posture as H-11: the exception must be written down and actually cover this instance; no inferred, generic, or stretched exceptions, and when in doubt the built-in check STILL fires. PRECEDENCE: this carve-out and H-11 (step 3.5) read the SAME ancestor declarations, so a declared rule + its exception must yield ONE consistent outcome for a given instance -- an exception that silences the H-11 M_ancestor_convention_violation finding silences this built-in convention FIX too, and vice versa; they must never contradict (H-11 silent while the built-in FIX still fires is exactly the bug this carve-out removes).`
    : ``

  const standardsPaths = Array.isArray(f.standardsPaths) ? f.standardsPaths : []
  const standardsClause = standardsPaths.length > 0
    ? `USER-AUTHORED STANDARDS. Read each standards file, nearest-layer first: ${standardsPaths.map((p) => `"${p}"`).join(', ')}. Each is a *-standards.md carrying a fenced \`standards_set:\` block whose \`criteria[]\` are the project's or user's own opinions for this artifact type. Apply ONLY criteria whose \`statement\` you can quote VERBATIM from the standards file -- same rule-extraction posture as the ancestor-convention check: no inferred rules, no generic best-practice, no "spirit of" a criterion; if you cannot quote the statement verbatim, do NOT raise the finding. SKIP any criterion whose \`enforcement\` is \`mechanical\` -- those are the audit.py validator's job (it runs them under --config), not yours; you evaluate only judgment criteria (enforcement \`judgment\` or absent). For each violated criterion emit group "Hygiene", taxonomy N_user_standard_violation, severity taken from the criterion's declared \`severity\` (fail -> FAIL, info -> INFO, judgment -> JUDGMENT), anchored on the target line that violates it. The \`message\` MUST carry (a) the verbatim criterion statement, (b) the criterion \`id\`, and (c) the source standards-file path. Disposition is assigned in step 6 from the severity: a fail-severity violation is SERIOUS (a hard user-declared rule the auditor cannot mechanically satisfy -- surface at the top, never auto-fix), an info-severity note is IMPROVE (one-line pitch), a judgment-severity call is JUDGMENT (surfaced for review).`
    : `No user-authored standards files were supplied; do NOT apply any user standards and emit no N_user_standard_violation findings.`

  const disabledCriteria = Array.isArray(input.disabledCriteria) ? input.disabledCriteria : []
  const disabledClause = disabledCriteria.length > 0
    ? `DISABLED CRITERIA. The run configuration switched these optional criterion/rule ids OFF: ${disabledCriteria.map((d) => `"${d}"`).join(', ')}. SUPPRESS any finding whose criterion id or rule id matches one in that list -- do not emit it and do not count it toward the verdict. That list only ever names OPTIONAL ids; architectural (schema/contract) and integrity (frontmatter, reachability, convention) checks are NEVER in it, so never suppress one of those on account of this list.`
    : `No criteria were disabled for this run; apply every criterion normally.`

  const readStep = isRef
    ? `Read the target reference document. Note the OWNING skill (the \`skills/<name>/\` directory it sits under) and read that skill's SKILL.md too -- you need its index entry for this file and its section headings to judge SR-1 and the back-reference check. Count lines and estimate tokens (~chars/4). Report \`skill_name\` = the owning skill's name and \`skill_type\` = "(skill reference)".`
    : `Read the target SKILL.md. Note its frontmatter name + skill-type. Count lines and estimate tokens (~chars/4).`

  const criteriaStep = isRef
    ? `Apply the SKILL-REFERENCE criteria (skill-standards.md section 10; this recap is self-contained -- do NOT load any framework doc). Judge the document's PROSE. You may read source ONLY to verify a claim the document already makes; a defect you find in the described system is a CODE-REVIEW finding and is OUT OF SCOPE for this lane -- do not report it.
   - SR-1 inbound anchor integrity (criterion "SR-1", taxonomy O_broken_inbound_anchor, group ADP): an inbound citation that would BREAK must not break. TWO forms break, and only these are severity FAIL: an ANCHOR LINK into this document (\`<path>#<anchor>\`), and a citation quoting one of this document's headings VERBATIM. NOT A VIOLATION, and this is the load-bearing half: an informal prose pointer naming a section approximately -- different case, a prefix, a paraphrase, a bolded inline label rather than a heading -- that resolves UNAMBIGUOUSLY to exactly one place in the document. Nothing is broken and no reader is misled, so raising it is noise; it is this criterion's dominant false-positive mode. A pointer that is genuinely AMBIGUOUS (it could mean two sections, or none) IS a violation, at severity JUDGMENT. Method: collect this document's headings, then grep the owning skill directory (and the wider repo where cheap) for citations naming this file, and CLASSIFY EACH CITATION BY FORM before judging it. In REVIEW MODE the pre-image makes the FAIL case direct: a heading present in the pre-image and absent now, with >=1 inbound anchor link or verbatim quote naming it, is the canonical instance. Anchor the finding on the CITING file and line and NAME the current heading as the correct target -- do NOT raise it without both halves. Disposition FIX when the successor heading is identified (a mechanical citer update), IMPROVE when the heading was deleted outright with no successor. Do NOT restate the corpus-wide OUTBOUND link scan here; that is the audit_references lane.
   - SR-2 internal consistency (criterion "SR-2", taxonomy P_internal_contradiction, group Hygiene, severity FAIL): the document must not contradict itself. For each load-bearing claim -- an imperative, a guarantee, a bound, a count, a name -- check whether another passage asserts its negation, a different value, or an example that breaks it. The \`message\` MUST quote BOTH passages with line numbers; a one-sided suspicion is not an SR-2 finding. Disposition IMPROVE by default (you usually cannot tell which side is true, and picking wrong writes a confident falsehood over a visible conflict); FIX when one side is FALSIFIED by your own verified reading; SERIOUS when the contradiction concerns a protective mechanism, since one reading leaves an invariant unguarded.
   - SR-3 claim calibration (criterion "SR-3", taxonomy Q_overstated_claim, group Hygiene, severity JUDGMENT): an unhedged universal or guarantee about BEHAVIOR -- "always", "never", "every", "cannot", "guaranteed", "impossible", "silently handles" -- needs a basis the reader can reach: a named source (a path, a test, a command), or the mechanism that makes it true, stated in the document. No basis and no hedge is a violation, anchored on the claim. SCOPE GUARD, load-bearing -- THREE genres, and only the FIRST is in scope: (1) a CLAIM reports what the system DOES ("this file can never drift") -- in scope; (2) an INSTRUCTION tells the reader what to do ("never hand-edit this file") -- OUT of scope, the document is entitled to state a rule absolutely; (3) a NORMATIVE DESIGN PRINCIPLE or declared INVARIANT states what the system MUST hold, as a rule it holds itself to rather than a report of observed behavior ("a pipeline never wildcard-adds"; "every stochastic decision is seeded deterministically") -- OUT of scope. A document whose declared genre is principles (a Principle / Why / Embodied-by structure, an "invariants" section) is MADE of genre 3, so treating those as claims turns the whole document into findings. Genres 2 and 3 are this criterion's two measured false-positive modes. When a declared invariant is contradicted by the document's own text, that is SR-2, not SR-3. CONSEQUENCE BAR: even inside genre 1, raise it ONLY when a reader who believed the claim AS STATED could act wrongly -- rely on a guarantee that does not hold, or skip a check the claim says is unnecessary. A rhetorical universal inside an argument, where the argument survives the qualification, is NOT a finding: the remediation would be cosmetic and the reader was never going to be misled. Disposition IMPROVE.
   - SR-4 reader fit (criterion "SR-4", taxonomy R_maintainer_only_material, group CCP, severity JUDGMENT): the content must be what the READER needs when the situation that loads this L3 document fires. Material whose ONLY reader is someone maintaining the document's own PRODUCTION PIPELINE belongs elsewhere -- decision provenance and derivation history (home: the co-located CLAUDE.md), regeneration instructions naming a tool the reader's install does not contain, generator plumbing colocated with the artifact for build convenience. SCOPE GUARD: guidance addressed to someone maintaining the SYSTEM the document describes is content the reader NEEDS, not maintainer-only material -- only the document's own production pipeline is in scope, and reading the rule more broadly than that is this criterion's measured false-positive mode. Per section, name the reader and the situation that loads it; a section whose only reader is someone editing the document's production pipeline is a violation. Sharpest signal: an instruction the reader cannot execute. Disposition IMPROVE -- a RELOCATION, never a silent deletion; the content has a correct home, so name it.
   - Back-reference (adp_back_reference, taxonomy H_adp_back_reference, group ADP, severity FAIL): this document must be one hop deep from its owning SKILL.md and must NOT cite that SKILL.md's sections (a back-reference is a cycle). Disposition FIX (mechanical rewrite).
   - NOT APPLICABLE to this subject, do not emit: taxonomies A, B, C, D, E (SKILL.md contract rows -- a reference has no frontmatter, no type and no schema), F and G (the L2->L3 placement/split call belongs to the owning SKILL.md's own audit), I (that row is about provenance leaking into a SKILL.md; provenance inside a reference is SR-4), J (hygiene thresholds -- a reference being long is the point of L3), L (reachability is owned by the SKILL.md subject, which can see the index).`
    : `Apply the cohesion-principle judgment for SKILL.md (this recap is self-contained; do NOT load any framework doc):
   - CCP (ccp_placement): SKILL.md content belongs here only when it changes WITH the skill's contract. Project-convention content (local code-review rules, project tool prefs — content that changes with project conventions) is misallocated; its home is the co-located CLAUDE.md. A violation is taxonomy F (DISCUSS), group CCP, severity JUDGMENT.
   - decision_provenance: Dec-N entries, "audit-finding" tags, dated decision-log lines change with audits, not the contract. In a SKILL.md body they are a FAIL — taxonomy I (FIX -- mechanical move to the co-located CLAUDE.md), group CCP. Detect Dec-\\d patterns / "audit-finding" / "decision log" markers.
   - CRP (crp_placement): SKILL.md is read together; references/ are loaded on-demand for DISTINCT sub-tasks. Body length over ~500 lines / ~3000 tokens is a SIGNAL to evaluate a split, never a verdict by itself. Only when sections genuinely serve different reading tasks AND the body is over threshold is it taxonomy G (DISCUSS), group CRP, JUDGMENT. A stub whose reference is always co-loaded is a tool-call doubling, not a win — do not propose that split.
   - ADP (adp_back_reference): reference docs under this skill's references/ must be one hop deep from SKILL.md and must NOT cite SKILL.md sections (a back-reference is a cycle). Read each references/*.md (if any) and check for back-citations to this SKILL.md. A back-reference is a FAIL — taxonomy H (FIX -- mechanical rewrite), group ADP.
   - Load-graph routing (references_reachable_from_skill_md, judgment half): the validator already surfaces missing edges mechanically; the lane adds only the keyword-adequacy call — for content a reference doc owns (its headings, entity names, script names), do the SKILL.md index entry's keywords carry the exact terms a searcher would use? A clear routing gap is taxonomy L (IMPROVE), group ADP, severity JUDGMENT.`

  const hygieneStep = isRef
    ? `Hygiene thresholds do NOT apply to a reference document (taxonomy J is out of scope for this subject) -- L3 content is where the long-form material is supposed to live. Emit no size finding.`
    : `Hygiene: body over ~500 lines or ~3000 tokens -> one INFO finding, group Hygiene, taxonomy J — a CRP-evaluation prompt, never a FAIL on its own; disposition IMPROVE when a concrete extraction candidate can be named, else SILENT.`

  const wrongTypeStep = isRef
    ? `Wrong-type signal does NOT apply to a reference document (it declares no type). Emit no taxonomy C finding.`
    : `Wrong-type signal (taxonomy C): only raise if the validator's type-specific rows or the body shape clearly contradict the declared skill-type. Emit as group Schema, severity JUDGMENT, disposition IMPROVE, and note that classify.py confirmation is deferred to the Q&A gate (the lane does NOT run classify.py).`

  const subjectLabel = isRef ? 'skill reference document' : 'SKILL.md'

  return `You are ONE lane of a ${subjectLabel} audit. Audit exactly one file and return structured findings. This is DETECTION ONLY — do not modify any file.

Target:    ${f.path}
Subject:   ${subject}   (${subjectLabel})
SkillType: ${isRef ? '(skill reference -- no declared type)' : (f.skillType || '(read from frontmatter)')}

${routingClause}

Steps:
1. ${readStep}
2. ${schemaClause}
3. ${criteriaStep}
3.5. ${ancestorConventionsClause}
3.6. ${standardsClause}
3.7. ${disabledClause}
4. ${hygieneStep}
5. ${wrongTypeStep}
6. DISPOSITION CLASSIFIER. Assign EVERY non-PASS finding a taxonomy id (A-N for a SKILL.md subject, O-R plus H/M/N/K for a skill-reference subject; M_ancestor_convention_violation is the H-11 ancestor-convention finding from step 3.5, group Hygiene; N_user_standard_violation is the user-authored-standards finding from step 3.6, group Hygiene, disposition driven by the criterion's declared severity -- fail -> SERIOUS, info -> IMPROVE, judgment -> JUDGMENT) and one of four dispositions -- FIX / SERIOUS / IMPROVE / SILENT (K -> SPECIAL). The taxonomy default bucket is a starting point only; decide instance-level against these predicates.

   Classifier prod (read this FIRST -- it overrides your default caution): You are biased toward conservatism; the user's time and attention are the scarce resources; source control and CL review are the safety net. If the edit very likely improves the doc, apply it.

   Master razor: FIX = anything decidable by VERIFIED FACTS plus DOCUMENTED PROJECT CONVENTIONS. Reserve IMPROVE for where no fact and no convention decides. The bar for FIX is: would a reasonable owner, seeing this diff in CL review, accept it without discussion? "Very likely improving" clears it.

   FIX (auto-applied; lands in a reviewable CL): a correction against a verified fact (a broken link/path with an identified target; a count/list/attribution/signature; a semantic claim corrected from a verified code reading); deletion of FALSIFIED content; a convention-violation fix (non-ASCII look-alike, hardcoded absolute/foreign-machine path, drifted line number, dedup under the summarize-and-reference rule -- REMINDER PLUS REFERENCE, a dozen tokens or less, else reference-only); a trim of a default / obvious-to-any-agent content (be aggressive). Mechanical skill fixes: A missing-default frontmatter, H back-reference rewrite, I Dec-N move. Loss-free-deletion guard ALWAYS before removing a duplicate/section: fold any local delta into the SSOT or summary line FIRST.${builtinConventionExceptionClause}
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

   Declined-opportunity ledger: if the SKILL.md frontmatter carries an \`md-audit-declined:\` list (suffixed taxonomy ids or short finding keys), do NOT re-raise an IMPROVE finding the user already declined for that file -- honor it exactly like references-audit honors \`references-audit-allow-stale\`. A new or materially different finding still fires.
   PASS / INFO / JUDGMENT findings that need no remediation get taxonomy "none" and bucket "NONE".
   For each FIX/SERIOUS/IMPROVE/SPECIAL finding write a concrete \`remediation\` (FIX = the edit it will apply; SERIOUS = the one-line top-of-report summary; IMPROVE = the single one-line pitch), with line refs.
6.5. ATTRIBUTABILITY. ${reviewClause}
7. Verdict: NOT-AUDITED if the artifact-shape decline fired (the target is neither a SKILL.md nor a skill reference document, so the criteria were never applied) — checked FIRST and overriding everything else, in BOTH modes; never COMPLIANT or DIFF-CLEAN, which would assert a clean file nobody read. Otherwise NON-COMPLIANT if ANY finding has severity FAIL; otherwise COMPLIANT. INFO/JUDGMENT never gate. Disposition is orthogonal to the verdict.

Idempotency matters: apply the fixed criteria and taxonomy deterministically. Do not invent findings; report only what the criteria actually surface. Return the structured object.`
}

phase('Audit')
const perFile = await parallel(input.files.map((f) => () =>
  // Default lane tier: opus at high effort. Detection is the audits' judgment
  // core — criteria application warrants the judge tier, explicitly pinned.
  agent(lanePrompt(f), {
    label: `audit:${f.path.split(/[\\/]/).slice(-2).join('/')}`,
    phase: 'Audit',
    model: 'opus',
    effort: 'high',
    schema: FILE_FINDINGS_SCHEMA,
  }).then((r) => ({ ...r, path: f.path }))
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
  ? `Reviewed ${results.length}/${input.files.length} skill-artifact files (SKILL.md + skill references) — ${totals.diffClean} DIFF-CLEAN, ${totals.nonCompliant} NON-COMPLIANT${declined}, ${totals.fail} attributable FAIL; dispositions SERIOUS=${totals.serious} FIX=${totals.fix} IMPROVE=${totals.improve} (${totals.suppressed} pre-existing finding(s) suppressed as not caused by this change; SILENT=${totals.silent} omitted)`
  : `Audited ${results.length}/${input.files.length} skill-artifact files (SKILL.md + skill references) — ${totals.nonCompliant} NON-COMPLIANT${declined}, ${totals.fail} FAIL findings; dispositions SERIOUS=${totals.serious} FIX=${totals.fix} IMPROVE=${totals.improve} (SILENT=${totals.silent} omitted)`)

return { perFile: results, totals, review }
