---
_schema_version: 1
name: skill-audit
author: christina
skill-type: audit-skill
description: Use when md-audit dispatches a SKILL.md audit (contract, cohesion, roster/hierarchy), fanning multi-file runs via the Workflow tool. Do NOT use for CLAUDE.md.
disable-model-invocation: true
user-invocable: false
argument-hint: "[ (none) | list | <path> | <numbers> [fast] | roster [path|-] | hierarchy [path|-] ]"
---

# Skill Audit

## Plugin version (always echo first)

!`uv run python "${CLAUDE_PLUGIN_ROOT}/scripts/print_version.py"`

The first line of your response MUST be the `Running ...` line printed above. This gives the user immediate confirmation of which plugin version actually executed (the slash registry can lag the on-disk cache; this is the only reliable signal).

skill-audit (reached via `/md-audit skill`) is the umbrella for **skill analysis tooling** in skills-kit. It hosts three procedures over the same skill corpus (User skills + Project skills + installed Plugin skills, discovered via the shared `skills_kit_lib.corpus` module):

- **Per-skill audit** -- the namesake operation; checks one SKILL.md against the framework's required blocks, cohesion principles, and hygiene rules; classifies findings into a taxonomy and dispatches remediation.
- **Roster** -- corpus-wide markdown inventory grouped by location and type.
- **Hierarchy** -- corpus-wide interactive HTML browser with frontmatter columns and skill-type hover tooltips.

## Framework

The per-skill audit operationalizes the **skill-md-audit** audit-kind under the shared audit framework. The canonical glossary -- `subject`, `primitive`, `composition`, `discovery`, `audit-kind`, `rule`, `finding`, `severity`, `taxonomy`, `bucket`, `corpus`, `scaffolding` -- lives at `skills-kit:md-audit/references/audit-framework.md`, with the data model at `skills-kit:md-audit/references/audit-framework.yaml`. The sibling skill `references-audit` (via `/md-audit references`) operationalizes the other audit-kind defined in the same framework. Definitions live in the framework; this file describes only how the audit applies them.

In framework terms, the per-skill audit procedure is:

- **Subject:** a `skill_md` primitive inside a `skill` composition (one `SKILL.md` per pass).
- **Primitives consumed:** `skill_md` and `yaml` (the embedded contract block).
- **Scaffolding:** `python -m skills_kit_lib.audit` (run from the plugin root) for mechanical schema validation; agent judgment for CCP / CRP / ADP placement.
- **Rules:** canonical definitions live in this skill's own `criteria:` block below (single source of truth per rule). The framework registry at `audit-framework.yaml::audit_kinds.skill_md_audit.rules_per_composition.skill` catalogs the bindings by id only.
- **Taxonomy + dispositions:** the A-L categories below; each finding is dispositioned instance-level FIX / SERIOUS / IMPROVE / SILENT (K -> SPECIAL) by the detect.js classifier.

The roster and hierarchy procedures share the same corpus subject but do not exercise the findings/remediation machinery -- they are inventory surfaces over the same primitives.

```yaml
audit_skill:
  _schema_version: "1"
  identity: "Audit Claude Code SKILL.md files against the skill-authoring framework -- required blocks, cohesion placement, hygiene -- and dispatch remediations by finding category. Includes corpus-wide roster and hierarchy reports over the same subject."
  scope:
    covers:
      - "auditing a single SKILL.md against its declared type contract (schema validation via audit.py)"
      - "auditing a SKILL.md against CCP / CRP / ADP placement rules (judgment-based from cohesion-principles)"
      - "auditing description hygiene (length, directive form, exclusion clause) and decision-provenance leakage"
      - "classifying each per-file finding into one of four dispositions instance-level (FIX auto-applied / SERIOUS surfaced-at-top / IMPROVE count-plus-one-liners / SILENT not-surfaced; K -> SPECIAL)"
      - "listing all SKILL.md files visible from cwd (the cwd-relative discover.py helper for index-based selection)"
      - "generating a corpus-wide markdown roster grouped by location and skill-type (the roster procedure)"
      - "generating an interactive HTML hierarchy of the corpus with frontmatter columns and skill-type tooltips (the hierarchy procedure)"
    excludes:
      - "auditing CLAUDE.md files (use /md-audit claude-md)"
      - "auditing reference docs in isolation (audited transitively via the SKILL.md they belong to)"
      - "auditing cross-references between skills (use /md-audit references)"
  subject:
    what: "Claude Code SKILL.md files from the User / Project / installed-Plugin corpus, evaluated against the skill-authoring framework's required-blocks / cohesion / hygiene contract."
    subject_type: "corpus"
  criteria:
    - id: "required_frontmatter"
      name: "Required frontmatter fields present and well-formed"
      keywords: ["frontmatter", "name field", "description field", "skill-type field", "required keys"]
      summary: "SKILL.md frontmatter must declare name, description, and a valid skill-type. Name conforms to the lowercase-hyphen pattern; description fits within 160 characters and uses the directive form."
      severity: "FAIL"
      detail: "Mechanical check via audit.py universal-rules section. Missing or malformed frontmatter blocks all downstream analysis."
    - id: "description_quality"
      name: "Description directive form and exclusion clause"
      keywords: ["description", "directive form", "exclusion clause", "do not use", "use when"]
      summary: "Description must start with 'Use when...' or 'Invoke when...' (directive form) and carry a 'Do NOT use for...' exclusion clause that contrasts the skill with neighbors."
      severity: "FAIL"
      detail: "Captured by audit.py mechanically. Vague or under-specified descriptions fail to route correctly when the model picks among skills."
    - id: "yaml_contract_block"
      name: "Body YAML contract block parses against declared skill-type schema"
      keywords: ["yaml contract", "schema validation", "audit.py", "contract block"]
      summary: "The SKILL.md body must contain a fenced yaml block with the declared skill-type's root key, and the block must validate against the type's schema in schemas.py."
      severity: "FAIL"
      detail: "Mechanical schema validator. Captures missing required keys, wrong types, forbidden cross-type keys (mixed-type drift), and contract floor violations."
    - id: "mixed_type_signal"
      name: "No mixed-type drift (single contract root)"
      keywords: ["mixed-type", "contract root", "drift signal", "single type"]
      summary: "Exactly one canonical contract root key appears in the body YAML. Multiple roots signal the skill grew across type boundaries and should split."
      severity: "FAIL"
      detail: "Mechanical check by detect_mixed_type_yaml. A judgment finding when the orientation-summary exception applies (a domain-skill's orientation may include a single technique-flavored summary section without triggering the signal)."
    - id: "ccp_placement"
      name: "CCP -- content changes for the same reason"
      keywords: ["ccp", "common closure principle", "content allocation", "change cadence"]
      summary: "SKILL.md content belongs there only when it changes with the skill's contract. Project-convention content (changes with project conventions) belongs in the co-located CLAUDE.md, not SKILL.md."
      severity: "JUDGMENT"
      detail: "Judgment call per cohesion-principles per_artifact_role.skill_md.audit_rules. The agent reads the body and asks: does this content change with the skill's contract or with project conventions?"
    - id: "crp_placement"
      name: "CRP -- read-together rule for SKILL.md vs references/"
      keywords: ["crp", "common reuse principle", "progressive disclosure", "reference split"]
      summary: "Content in SKILL.md is read together; content in references/ is loaded on-demand for distinct sub-tasks. Splitting must serve different reading tasks, not arbitrary size reduction."
      severity: "JUDGMENT"
      detail: "Judgment call. Body length is a signal that the split deserves evaluation (line/token thresholds), not a verdict. A stub-with-always-co-loaded-reference is a tool-call doubling, not a context-efficiency win."
    - id: "adp_back_reference"
      name: "ADP -- reference graph is a DAG with no SKILL.md back-references"
      keywords: ["adp", "acyclic dependencies", "back-reference", "reference graph"]
      summary: "Reference docs in references/ must be one hop deep from SKILL.md and must not cite SKILL.md sections. A back-reference creates a cycle and a context-loading hazard."
      severity: "FAIL"
      detail: "Partially mechanical via audit.py one-hop-deep check; judgment for back-reference detection inside reference body text."
    - id: "references_reachable_from_skill_md"
      name: "Load-graph coverage -- every skill member is reachable from SKILL.md"
      keywords: ["orphaned reference", "load graph", "reachability", "index coverage", "unlinked directory", "dangling index entry", "missing edge", "undiscoverable member"]
      summary: "Every file under references/ carries an edge from SKILL.md (citation or index entry), every content-bearing member directory (tests/, scripts/, templates/, ...) is named somewhere in SKILL.md, and every structured index path resolves on disk."
      severity: "FAIL"
      detail: "Mechanical via audit.py (check_references_reachable_from_skill_md). FAIL: an .md under references/ reachable by NO path (true orphan -- an agent with the skill loaded cannot find it), or a structured index/members path pointing at a missing file. JUDGMENT: an .md reachable only via a sibling reference (two hops -- index keywords cannot route to it), a non-md references/ file with no edge, or a member directory with no SKILL.md edge (may be an internal helper). The judgment half -- whether an index entry's keywords carry the terms a searcher would actually use -- is not mechanically decidable; it is applied by reading (see taxonomy L). Un-parked from audit-framework.yaml::future_rules 2026-07-15 (home-domain incident)."
    - id: "decision_provenance"
      name: "Decision provenance does not bleed into SKILL.md"
      keywords: ["decision provenance", "Dec-N entries", "audit-finding logs", "ccp violation"]
      summary: "Dec-N entries, audit-finding logs, and decision history change with audits, not with the skill's contract. Their home is the co-located CLAUDE.md, not SKILL.md."
      severity: "FAIL"
      detail: "Detected by scanning the SKILL.md body for Dec-N patterns or 'audit-finding' / 'decision log' markers. Always a CCP failure -- the content changes for a different reason than the skill's contract."
    - id: "hygiene_thresholds"
      name: "Hygiene -- body line and token thresholds"
      keywords: ["hygiene", "line count", "token count", "progressive disclosure threshold"]
      summary: "Body length above 500 lines or 3000 tokens is a signal to evaluate splitting; the threshold is informational, not a verdict."
      severity: "INFO"
      detail: "Mechanical line/token count from audit.py. Surfaces a CRP-evaluation prompt but never gates compliance on its own."
  taxonomy:
    - id: "A_missing_required_frontmatter"
      name: "Missing or malformed required frontmatter field"
      keywords: ["missing frontmatter", "required field", "frontmatter fail", "mechanical fix"]
      detection_signal: "audit.py FAIL on a universal-rules row (frontmatter.name, frontmatter.description, skill-type value, name charset, etc.)."
      default_remediation: "Add the missing field with a sensible default (e.g. derive name from directory) -- decidable by convention -> FIX. A field requiring authorial judgment (description) routes to IMPROVE (offer as a one-liner)."
      bucket: "FIX"
    - id: "B_description_quality"
      name: "Description fails directive-form / exclusion-clause / length checks"
      keywords: ["description", "directive form", "use when", "exclusion clause", "160 char limit"]
      detection_signal: "audit.py FAIL on description length (>160 chars), missing 'Use when' / 'Invoke when' prefix, or missing 'Do NOT use for' exclusion clause."
      default_remediation: "Rewrite the description via a background agent to fit the directive form, name the trigger condition, and exclude a contrasting neighbor. Authorial content -> IMPROVE (offer the proposed text as a one-liner; user opts in)."
      bucket: "IMPROVE"
    - id: "C_wrong_skill_type"
      name: "Declared skill-type does not match content shape"
      keywords: ["wrong type", "skill-type mismatch", "classify.py", "single-type suggestion"]
      detection_signal: "Run classify.py; suggested type differs from declared skill-type with single-type confidence >= 2."
      default_remediation: "Propose the classify.py suggestion to the user with rationale; user confirms the type change. If user agrees, re-run audit against the new type's contract. A genuine authorial choice -> IMPROVE (one-line pitch; user opts in)."
      bucket: "IMPROVE"
    - id: "D_mixed_type_signal"
      name: "Multiple contract root keys or cross-type content drift"
      keywords: ["mixed-type", "drift", "boundary split", "multiple roots"]
      detection_signal: "detect_mixed_type_yaml returns >1 canonical root, OR mixed-type heuristic score >= 2 from audit.py."
      default_remediation: "Surface the cross-type signals to the user. Propose a split along the type boundary (e.g. extract the technique-flavored section into a sibling technique-skill). Structural move -> IMPROVE (one-line pitch; user decides whether to split or apply the orientation-summary exception)."
      bucket: "IMPROVE"
    - id: "E_schema_validation_failure"
      name: "Body YAML block fails schema validation"
      keywords: ["schema fail", "yaml validation", "required key missing", "forbidden key"]
      detection_signal: "audit.py reports YAML contract validation failure: missing required key, wrong type, list below min_len, forbidden key present."
      default_remediation: "Surface the specific failing rows; remediation depends on which row failed. A missing field with a sensible default -> FIX (add it). An authorial-choice row -> IMPROVE. A forbidden key indicates mixed-type drift (treat as D, IMPROVE)."
      bucket: "FIX"
    - id: "F_ccp_misallocation"
      name: "CCP violation -- project-convention content in SKILL.md"
      keywords: ["ccp", "project convention", "wrong home", "claude.md", "content allocation"]
      detection_signal: "Agent judgment from cohesion-principles per_artifact_role.skill_md.audit_rules. Body section changes with project conventions (e.g. local code-review rules, project-specific tool preferences) rather than the skill's contract."
      default_remediation: "Propose moving the misallocated section into the co-located CLAUDE.md (or the project root CLAUDE.md if the convention is project-wide). Structural move -> IMPROVE (one-line pitch; user opts in)."
      bucket: "IMPROVE"
    - id: "G_crp_violation"
      name: "CRP violation -- SKILL.md should split into references/"
      keywords: ["crp", "reference split", "progressive disclosure", "different reading tasks"]
      detection_signal: "Agent judgment. Body sections serve genuinely different reading tasks (e.g. usage docs + worked examples + deep-mechanic reference); body length is over thresholds; a CRP-passing split exists."
      default_remediation: "Propose an L2 -> L3 decomposition with concrete reference-doc paths and a triggering criterion per ref. Offerable (IMPROVE) only with a NAMED extraction candidate; a bare over-threshold nudge with none is SILENT. One-line pitch; user opts in before splitting."
      bucket: "IMPROVE"
    - id: "H_adp_back_reference"
      name: "Reference doc cites its own SKILL.md sections"
      keywords: ["adp", "back-reference", "cycle", "one-hop"]
      detection_signal: "Audit detects a `..in skills-kit:<this-skill>` reference inside a doc under this skill's references/ directory."
      default_remediation: "Rewrite the reference doc to not cite the SKILL.md (the load-graph flows one way: SKILL.md -> references). Inline the relevant context if the reference truly needs it. Mechanical convention fix -> FIX."
      bucket: "FIX"
    - id: "I_decision_provenance"
      name: "Dec-N entries or audit-finding logs in SKILL.md body"
      keywords: ["decision provenance", "Dec-N", "audit history", "ccp violation", "wrong home"]
      detection_signal: "Body contains Dec-N patterns (`Dec-1:`, `Dec-2:` etc.), 'audit-finding-N' tags, or decision-log entries dated by audit pass."
      default_remediation: "Move the Dec-N entries to the co-located CLAUDE.md (or create one if missing). The SKILL.md retains only the resulting rule, not the audit history that produced it. Mechanical CCP fix -> FIX."
      bucket: "FIX"
    - id: "J_hygiene_threshold"
      name: "Body over line / token threshold (CRP evaluation prompt)"
      keywords: ["hygiene", "line count", "token count", "size threshold"]
      detection_signal: "audit.py reports line count > 500 or token count > 3000. This is INFO severity -- a prompt to evaluate CRP, not a verdict."
      default_remediation: "Run the CRP test: do the body sections serve different reading tasks? If yes, route to G. If no, the larger SKILL.md is the correct answer (a bare over-threshold nudge with no named extraction candidate is SILENT)."
      bucket: "IMPROVE"
    - id: "K_unclassified"
      name: "Unclassified / special case"
      keywords: ["unclassified", "special case", "escape hatch", "K bucket"]
      detection_signal: "Finding does not match any A-J or L detection signal after deliberate attempt."
      default_remediation: "Surface to the user with the audit row that fired, attempted category matches, and reasons none fit. User proposes strategy."
      bucket: "SPECIAL"
    - id: "L_load_graph_gap"
      name: "Load-graph edge gap -- a member exists but SKILL.md cannot route to it"
      keywords: ["orphaned reference", "unlinked tests dir", "unlinked scripts dir", "index coverage", "keyword routing gap", "dangling index entry", "load graph", "undiscoverable", "missing edge"]
      detection_signal: "audit.py FAIL or JUDGMENT on a load-graph row (references_reachable_from_skill_md): an orphaned references/ file, a member directory with no SKILL.md edge, a two-hop-only reference, or an index entry pointing at a missing path. Judgment extension (not mechanically decidable): an index entry whose keywords omit the terms a searcher would use for content the doc owns -- test by picking each major contract the doc carries (its headings, entity names, script names) and checking those exact terms against the entry's keywords."
      default_remediation: "Add the missing edge: an index.references entry (or index.members / tools[].tests for scripts and test suites) with keywords drawn from the member's own headings and identifiers; fix or drop a dangling index path; append the missing searcher-terms to an under-keyworded entry. Deleting the member is the alternative when it is genuinely dead. IMPROVE by default (adding edges/keywords is a judgment); a dangling index path with an identified correct target is a mechanical FIX. An accepted orphan (internal helper, historical member) is SILENT."
      bucket: "IMPROVE"
    - id: "M_ancestor_convention_violation"
      name: "SKILL.md violates a convention an ancestor CLAUDE.md explicitly declares (H-11)"
      keywords: ["ancestor convention", "H-11", "ascii-only", "no absolute paths", "declared convention", "verbatim rule"]
      detection_signal: "H-11 hit: the SKILL.md violates a convention EXPLICITLY declared in an ancestor CLAUDE.md (loaded ambient), and the exact declared rule is quotable VERBATIM from that ancestor (no inferred / generic conventions). Fires only when ancestorClaudeMdPaths is supplied and non-empty; a skill with no ancestor CLAUDE.md above it emits nothing."
      default_remediation: "FIX for a mechanical correction against the documented convention (replace a non-ASCII look-alike, relativize a hardcoded absolute path, apply the stated formatting rule); the message carries the verbatim ancestor rule quote + the ancestor source path. SERIOUS when the violation reveals a real-world problem the rule exists to prevent (e.g. a committed secret an ancestor forbids) -- surfaced at the top, never auto-fixed."
      bucket: "FIX"
    - id: "N_user_standard_violation"
      name: "SKILL.md violates a user-authored standards criterion (standards_set)"
      keywords: ["user standard", "standards_set", "configurable standard", "verbatim criterion", "layered standards", "user opinion"]
      detection_signal: "A criterion from a resolved *-standards.md (standards_set) governing the skill_md primitive is violated, with the criterion statement quotable VERBATIM from the standards file. Judgment criteria only (enforcement judgment or absent); enforcement: mechanical criteria are audit.py's job under --config, not the lane's. Fires only when standardsPaths is supplied and non-empty; no standards files -> emits nothing. Suppressed when the criterion id is in disabledCriteria."
      default_remediation: "Disposition follows the criterion's declared severity: fail -> SERIOUS (a hard user-declared rule the auditor cannot mechanically satisfy -- surfaced at the top, never auto-fixed; the message carries the verbatim statement + criterion id + source standards-file path); info -> IMPROVE (one-line pitch); judgment -> JUDGMENT (surfaced for review). Unlike M ancestor conventions, an arbitrary user standard is not mechanically fixable, so N is never auto-applied."
      bucket: "SERIOUS"
  procedures:
    - id: "audit_skill_md"
      name: "Audit one SKILL.md against the framework and dispatch remediations"
      keywords: ["audit", "single-file audit", "namesake operation", "dispatch", "compliance verdict"]
      goal: "For each target SKILL.md, run mechanical and judgment-based checks against the framework's contract, classify findings into the taxonomy, assign each a disposition (FIX / SERIOUS / IMPROVE / SILENT; K -> SPECIAL), and emit a per-file compliance verdict (COMPLIANT or NON-COMPLIANT)."
      preconditions:
        - "The mechanical validator (skills_kit_lib.audit) is reachable via the plugin venv."
        - "The skill-md cohesion recap (CCP / CRP / ADP / decision-provenance) is embedded in the DETECT step and the detect.js lane prompt; no separate cohesion-principles load is required for the audit path."
        - "workflow/detect.js and workflow/remediate.js are present (used for the 2+-file fan-out)."
      steps:
        - n: 1
          action: "Resolve the audit target set from $ARGUMENTS. Empty -> cwd/SKILL.md if present, else stop. 'list' -> emit numbered list via discover.py and stop. Integers -> map to paths from last list output. Path -> use it directly. Strip any non-interactive token ('fast', '--fast', '--yes', '-y') first and set non_interactive accordingly (also set it if the user's prose expresses non-interactive intent, e.g. 'just apply everything, don't ask'). Strip the review token ('review', '--review') and set review=true (also set it if the user's prose expresses the intent, e.g. 'review my changes before I submit', 'audit the diff'); review is FALSE by default. Reject the combination review + non_interactive: 'propose instead of applying, but do not ask' resolves to doing nothing -- tell the user the two are mutually exclusive and stop. For each resolved target SKILL.md also capture ancestorClaudeMdPaths (for the H-11 ancestor-convention check): the FULL ancestor CLAUDE.md chain above the SKILL.md, enumerated deterministically -- starting from the SKILL.md's PARENT directory, walk up one directory at a time until (and including) the workspace root (the cwd when there is no enclosing project, otherwise the nearest ancestor containing a `.git` entry), Glob/stat `<dir>/CLAUDE.md` at each level, and collect every one that exists into a list ordered NEAREST-ANCESTOR FIRST, EXCLUDING the SKILL.md itself. This includes the skill's co-located CLAUDE.md when one exists. Empty when nothing sits above the skill dir. ALSO, ONCE per run (not per file), resolve the configurable standards via the plugin venv: (cd ${CLAUDE_PLUGIN_ROOT} && <venvPython> scripts/resolve_standards.py --project-root <workspace root> --primitive skill_md), and parse its JSON { disabled, thresholds, standards }. Keep a run-level `disabledCriteria` = the `disabled` array (the optional rule/criterion ids the user switched off) and, for every target SKILL.md, `standardsPaths` = the `standards.skill_md` array (the user-authored *-standards.md files governing this member's primitive). Both thread into the detect step. An empty or absent config yields empty lists, so the default behavior is unchanged."
          tool: "discover.py"
          input: "uv run python ${CLAUDE_PLUGIN_ROOT}/skills/skill-audit/scripts/discover.py [--json]"
          expected: "A resolved list of SKILL.md file paths + non_interactive flag."
          on_failure: "If no target resolves, surface cwd and stop. Do not improvise a target."
        - n: 2
          action: "DETECT phase (before-Q&A). Choose execution mode by file count -- this threshold equalizes the Workflow tool's per-run overhead. REVIEW MODE OVERRIDE: when review is TRUE the threshold is 1, so ALWAYS use the Workflow path even for a single file. Review mode gates a submit/publish, so its verdict must not depend on whatever model the session happens to be running; only the lane pins model+effort and enforces the schema. Never run a review-mode detect inline. ONE file (non-review): audit inline in the main loop (run the mechanical validator, now passing --config so audit.py drops disabled mechanical rows and overlays the resolved thresholds; apply the embedded skill-md cohesion recap; if standardsPaths is non-empty apply the user-authored standards inline exactly as the lane does -- read each standards file, apply ONLY criteria whose statement is quotable VERBATIM, SKIP any criterion whose enforcement is mechanical (audit.py --config owns those), emit each violation as group Hygiene, taxonomy N_user_standard_violation, severity from the criterion's declared severity (fail->FAIL, info->INFO, judgment->JUDGMENT), message carrying the verbatim statement + criterion id + source path; suppress any finding whose criterion/rule id is in disabledCriteria (never an architectural or integrity id); if ancestorClaudeMdPaths is non-empty run the H-11 ancestor-convention check inline exactly as the lane does -- read each ancestor CLAUDE.md, flag a SKILL.md violation ONLY when the declared rule can be quoted VERBATIM from the ancestor (no inferred/generic conventions), emit it as group Hygiene, taxonomy M_ancestor_convention_violation, FAIL, with the verbatim quote + ancestor source path in the message; exception awareness (applies to H-11 AND to the built-in non-ASCII / hardcoded-absolute-path convention FIX in the classifier): when an ancestor EXPLICITLY declares a scoped exception that covers the specific instance -- right file scope AND right content kind, e.g. 'ASCII only, except developer names in the contributors section may contain non-ASCII characters' -- do NOT flag that instance under either check; demote it to PASS/INFO and cite the verbatim exception quote + ancestor source path in the message. The one declared rule + exception governs both checks so they never contradict (H-11 silent while the built-in convention FIX still fires is exactly the bug this removes); no inferred or stretched exceptions, and when in doubt the check STILL fires. Classify each finding into taxonomy + bucket). TWO OR MORE files (or ANY count in review mode): call the Workflow tool with scriptPath ${CLAUDE_PLUGIN_ROOT}/skills/skill-audit/workflow/detect.js and args = { files:[{path,skillType,ancestorClaudeMdPaths,standardsPaths,preImagePath}], disabledCriteria:<run-level disabled id list>, review:<review bool>, refs:{pluginRoot, venvPython} }. The workflow fans one lane out per file and returns { perFile, totals, review }. In review mode YOU materialize each pre-image BEFORE calling the workflow (see the review-mode section) and pass its path; the workflow is VCS-agnostic and will not fetch anything itself. Detection only -- no file is edited in this phase. Cohesion recap (used by both the inline path and the detect.js lane): CCP = SKILL.md content belongs here only when it changes with the skill's contract (project-convention content -> co-located CLAUDE.md, taxonomy F); decision-provenance Dec-N / audit-finding logs are a CCP FAIL (taxonomy I, AUTO); CRP = SKILL.md is read together, references/ load on-demand for distinct sub-tasks, size is a signal not a verdict (taxonomy G/J); ADP = references/*.md are one hop deep and must not cite SKILL.md (back-reference FAIL, taxonomy H, AUTO); load-graph rows from the validator (orphaned reference, unlinked member dir, dangling index edge, two-hop-only reference) route to taxonomy L (DISCUSS), as does the judgment call that an index entry's keywords omit the terms a searcher would use; H-11 = when ancestorClaudeMdPaths is supplied, a SKILL.md violation of a convention EXPLICITLY declared in an ancestor CLAUDE.md (quotable VERBATIM -- no inferred/generic conventions) is a FAIL, taxonomy M (group Hygiene, FIX default), with the verbatim quote + ancestor source path in the message -- UNLESS the ancestor also declares an explicit scoped exception covering the specific instance (right file scope + content kind), which suppresses both H-11 and the built-in non-ASCII / hardcoded-path convention FIX so the two never contradict."
          tool: "Workflow | inline"
          input: "detect.js args.refs: pluginRoot=${CLAUDE_PLUGIN_ROOT}; venvPython=<plugin venv python>. The validator is run as: (cd ${CLAUDE_PLUGIN_ROOT} && <venvPython> -m skills_kit_lib.audit <path> --json --config) -- --config makes audit.py honor the resolved config (drop disabled mechanical rows, overlay thresholds)."
          expected: "Structured per-file findings (group, severity, criterion, message, line, taxonomy, bucket, remediation) + per-file verdict."
          on_failure: "If the validator is unavailable, mark the Schema group JUDGMENT ('validator unavailable') and continue with cohesion judgment only -- never fail a file for that."
        - n: 3
          action: "Render the per-file report (output_template), then the REPORT CONTRACT summary in three visible sections IN THIS ORDER, no hedging: (1) SERIOUS -- 'Found <N> serious issue(s) that require fixing' + a one-line summary each; never auto-fixed, never buried. (2) FIX -- normally the count auto-applied and landing in the reviewable remediation CL; in REVIEW MODE nothing is auto-applied, so render it as the count PROPOSED and awaiting the step-4 decision, never as applied. (3) IMPROVE -- 'Audit found <N> improvement opportunit(ies). Do you want to discuss them?' + one one-line pitch each. SILENT findings do NOT appear. Omit a section whose count is zero."
          expected: "Markdown report: per-file verdicts, then SERIOUS (summarized, top) / FIX (applied count) / IMPROVE (count + one-liners); SILENT omitted."
        - n: 4
          action: "Q&A GATE. If review is TRUE: NOTHING is auto-applied -- FIX is demoted from auto-apply to PROPOSED and goes to the user alongside IMPROVE/SPECIAL. Present proposals with AskUserQuestion offering accept-all / reject-all / custom instruction (use multiSelect when accept-some is the natural shape). Use judgement on grouping: batch fixes across files into ONE question when they are small and the files are related; split into several when the fixes are large or the files are unrelated. SERIOUS is surfaced at the top as always and still never auto-fixed. Review-mode declines write NOTHING to `md-audit-declined:` -- that ledger is IMPROVE-scoped and per-file-permanent, whereas a review decline usually means 'not in this change', and once the change lands the finding is in the next pre-image and stops being attributable anyway. Offer an explicit 'never flag this again for this file' only if the user asks for it, and only then write the ledger. If review is FALSE and non_interactive is FALSE (default): SERIOUS findings are surfaced summarized at the top, never auto-fixed; for each IMPROVE and SPECIAL finding the user opted to discuss, ask for a decision (apply as-proposed / skip / a refined instruction). Surface a tight grouped set; do not dump a giant list. For category C (wrong skill-type), this is where classify.py is run to confirm the suggestion before any type change. A declined IMPROVE is recorded in the SKILL.md's `md-audit-declined:` frontmatter so a re-audit does not re-pitch it. If non_interactive is TRUE: apply FIX findings, surface SERIOUS, and infer each IMPROVE/SPECIAL decision from the taxonomy's default_remediation plus the file content -- record each inferred decision in the final summary. FIX findings need no decision (they apply by definition); SILENT findings are never surfaced."
          expected: "SERIOUS summarized; a decision (explicit or inferred) attached to every IMPROVE/SPECIAL the user engaged; FIX applied."
        - n: 5
          action: "REMEDIATE phase (after-Q&A). Assemble per-file remediation lists from the decided findings (FIX=apply; IMPROVE/SPECIAL=per decision; SERIOUS never auto-applied; drop skips). Choose mode by how many FILES carry remediation work. ONE file: apply inline with Edit. TWO OR MORE files: call the Workflow tool with scriptPath ${CLAUDE_PLUGIN_ROOT}/skills/skill-audit/workflow/remediate.js and args = { perFile:[{path,remediations:[{criterion,taxonomy,bucket,line,instruction,decision}]}] }. One lane per file (disjoint skills never conflict; taxonomy H edits a references/*.md and taxonomy I moves Dec-N into the co-located CLAUDE.md, both under the same skill dir)."
          tool: "Workflow | inline"
          expected: "Edits applied to the target files; per-file applied/skipped/failed summary."
        - n: 6
          action: "Render the final summary: FIX applied per file, IMPROVE decisions, SERIOUS still-open (never auto-applied), any failures. Remind the user that re-running the audit should now reproduce a clean (or reduced-FAIL) verdict -- detection and remediation are separate passes, so the re-run is the verification step. Scope the verification re-run to the files that were actually MODIFIED by remediation -- results for untouched files stand; re-auditing them wastes runs."
          expected: "Closing summary; user can re-run /md-audit skill on the modified files to verify FAILs cleared."
      output_template: |
        ## <skill name> (<skill-type>) -- <file path>

        Lines: <N> / Tokens: <N> / Findings: <count by bucket>

        ### Schema (audit.py)
        [PASS|FAIL|JUDGMENT] <row>: <message>

        ### CCP (content allocation -- change cadence)
        [PASS|FAIL|JUDGMENT] <criterion>: <message>

        ### CRP (read-together -- progressive disclosure)
        [PASS|FAIL|JUDGMENT] <criterion>: <message>

        ### ADP (one-hop-deep, no back-references)
        [PASS|FAIL|JUDGMENT] <criterion>: <message>

        ### Hygiene (universal rules)
        [PASS|FAIL|INFO] <criterion>: <message>

        ### Compliance verdict

        <P> PASS / <F> FAIL / <I> INFO / <J> JUDGMENT-REQUIRED
        Verdict: COMPLIANT | NON-COMPLIANT

        ## Report (SERIOUS -> FIX -> IMPROVE; SILENT omitted, no hedging)

        ### SERIOUS -- Found <N> serious issue(s) that require fixing
        - <one-line summary per issue>   (never auto-fixed)

        ### FIX -- <N> applied (in the reviewable remediation CL)   [review mode: "<N> proposed" -- nothing is applied]
        - <criterion>: <what was corrected>

        ### IMPROVE -- Audit found <N> improvement opportunit(ies). Do you want to discuss them?
        - <criterion>: <one-line pitch>
      gotchas:
        - "audit.py is the canonical mechanical validator. Do not re-implement its checks here; consume its JSON output and present it under the Schema group. The cohesion-principle judgment (CCP/CRP/ADP) is what this skill adds on top."
        - "CCP for SKILL.md asks: does this content change with the skill's contract, or with project conventions? Project-convention content bleeding into SKILL.md is a CCP-fail; the content belongs in the co-located CLAUDE.md."
        - "Hygiene thresholds (line / token count) are CRP-evaluation signals, not verdicts. A SKILL.md over the threshold is not automatically NON-COMPLIANT; apply the CRP test (do sections serve different reading tasks?) before proposing a split."
        - "Idempotency: criteria, taxonomy, and bucket assignments are fixed. Same input produces the same verdict; do not re-rank or re-order findings session-to-session."
    - id: "generate_roster"
      name: "Generate corpus-wide markdown roster"
      keywords: ["roster", "markdown inventory", "corpus listing", "location by type", "report.py"]
      goal: "Produce a markdown roster of every SKILL.md in the corpus (User + Project + installed Plugins), grouped by location then by skill-type, with per-type implied frontmatter declared once."
      preconditions:
        - "report.py is reachable; PyYAML is available."
      steps:
        - n: 1
          action: "Parse $ARGUMENTS for an optional output path or '-' for stdout."
          expected: "Output destination resolved."
        - n: 2
          action: "Run report.py with the roster subcommand."
          tool: "report.py"
          input: "uv run python ${CLAUDE_PLUGIN_ROOT}/skills/skill-audit/scripts/report.py roster [out]"
          expected: "Markdown roster written to the resolved path (or stdout), with skill-bearing plugins only."
        - n: 3
          action: "Relay the resolved output path to the user."
          expected: "User can open the file."
      gotchas:
        - "Implied frontmatter is per (skill-type, variant). User-only technique-skills imply both disable-model-invocation true and user-invocable true; per-skill rows show only flags that DIFFER from the implied set."
        - "Project skills resolve relative to cwd. If invoked from outside a project tree the Project section will be empty; this is expected, not a bug."
    - id: "generate_hierarchy"
      name: "Generate corpus-wide interactive HTML hierarchy"
      keywords: ["hierarchy", "html", "interactive", "frontmatter columns", "skill-type tooltips"]
      goal: "Produce a self-contained HTML hierarchy of the corpus: collapsible <details> sections, one column per frontmatter key, skill-type hover tooltips."
      preconditions:
        - "report.py is reachable; PyYAML is available."
      steps:
        - n: 1
          action: "Parse $ARGUMENTS for an optional output path or '-' for stdout."
          expected: "Output destination resolved."
        - n: 2
          action: "Run report.py with the hierarchy subcommand."
          tool: "report.py"
          input: "uv run python ${CLAUDE_PLUGIN_ROOT}/skills/skill-audit/scripts/report.py hierarchy [out]"
          expected: "Single-file HTML hierarchy written to the resolved path (or stdout). Plugins with no skills are dropped."
        - n: 3
          action: "Relay the resolved output path to the user."
          expected: "User can open the HTML in a browser."
      gotchas:
        - "Column union is per-section, not corpus-wide. A frontmatter key used by only one project skill (e.g. `allowed-tools`) appears in the Project-skills table only."
        - "Built-in slash commands without on-disk SKILL.md (e.g. /loop, /init, /review) do not appear in the hierarchy. This is correct -- they are not authored skills under any discovery root."
        - "The hierarchy does NOT apply the per-type implied-frontmatter convention from the roster -- every frontmatter key is shown as its own column."
  # Disposition mapping (four-disposition model): structural lanes retained for
  # schema stability across audit members. auto = FIX categories (auto-applied;
  # land in the reviewable CL). discuss = SERIOUS (surfaced at top, never auto) +
  # IMPROVE (count + one-liners, opt-in) categories, disposition noted per entry.
  # special = K escape hatch. The final per-finding disposition is assigned
  # instance-level by the detect.js classifier; these defaults are the start.
  remediations:
    auto:
      - category: "A_missing_required_frontmatter"
        procedure: "[FIX default] Add the missing field with a sensible default. For 'name', derive from the directory; for 'skill-type', use classify.py's suggestion if single-type. Description and other authorial fields are NOT FIX -- route to B / IMPROVE."
        agent_template: "Background agent receives file path + missing field name + default value; applies the edit; reports back."
      - category: "H_adp_back_reference"
        procedure: "[FIX] Open the cited reference doc; rewrite the back-citing sentence to inline the necessary context instead of referencing back to SKILL.md. Confirm the load-graph flows one way after the edit."
        agent_template: "Background agent receives reference doc path + back-citing line; rewrites to remove the back-reference; reports back."
      - category: "I_decision_provenance"
        procedure: "[FIX] Identify Dec-N entries / audit-finding tags in SKILL.md. Move them to the co-located CLAUDE.md (create the CLAUDE.md if absent). Retain only the resulting rule in SKILL.md, not the audit history."
        agent_template: "Background agent receives SKILL.md path + Dec-N line ranges; cuts from SKILL.md, appends to CLAUDE.md; reports back."
      - category: "E_schema_validation_failure"
        procedure: "[FIX default] Add a missing field with a sensible default (decidable by convention). An authorial-choice row -> IMPROVE; a forbidden key indicates mixed-type drift -> treat as D (IMPROVE). Route per row."
        agent_template: "Background agent receives the failing rows; adds missing-default fields; reports authorial / forbidden-key rows back for the IMPROVE lane."
      - category: "M_ancestor_convention_violation"
        procedure: "[FIX default] Apply the mechanical correction that satisfies the verbatim-quoted ancestor convention (replace the non-ASCII look-alike, relativize the absolute path, apply the stated formatting rule). SERIOUS instead when the violation reveals a real-world problem the rule exists to prevent (e.g. a committed secret) -- surface at the top, never auto-fix."
        agent_template: "Background agent receives the SKILL.md line + the verbatim ancestor rule quote + the ancestor source path; applies the convention-satisfying edit, or escalates a real-world-problem violation to the SERIOUS surface."
    discuss:
      - category: "B_description_quality"
        procedure: "[IMPROVE] Background agent proposes a rewritten description. Offer as a one-line pitch; apply only after user opts in."
      - category: "C_wrong_skill_type"
        procedure: "[IMPROVE] Surface classify.py's suggestion with rationale. One-line pitch; if the user opts in, confirm the type change and re-run audit against the new type's contract before finalizing."
      - category: "D_mixed_type_signal"
        procedure: "[IMPROVE] Surface the cross-type signals (which root keys, which structural markers). Propose a split along the type boundary OR confirm the orientation-summary exception applies (a domain-skill's orientation may include one technique-flavored summary). One-line pitch; user decides."
      - category: "F_ccp_misallocation"
        procedure: "[IMPROVE] Propose the destination (co-located CLAUDE.md or project root CLAUDE.md). One-line pitch; user opts in, agent applies."
      - category: "G_crp_violation"
        procedure: "[IMPROVE] Propose an L2 -> L3 decomposition with explicit reference-doc paths and triggering criteria per ref -- offerable only with a NAMED extraction candidate (else SILENT). One-line pitch; user opts in before splitting."
      - category: "J_hygiene_threshold"
        procedure: "[IMPROVE] Run the CRP test (do the body sections serve different reading tasks?). If yes and a concrete extraction candidate can be named, escalate to G; if no candidate, SILENT (the larger SKILL.md is correct)."
      - category: "L_load_graph_gap"
        procedure: "[IMPROVE default] Propose the concrete edge (index entry / members entry / tools[].tests / citation) with keywords drawn from the member's own headings and identifiers, or propose deleting a genuinely dead member. A dangling index path with an identified correct target is a mechanical FIX; an accepted internal-helper orphan is SILENT. One-line pitch for the judgment cases; user opts in."
      - category: "N_user_standard_violation"
        procedure: "[severity-driven; never auto-applied] fail -> SERIOUS (surface at the top, never auto-fixed -- the auditor cannot mechanically satisfy an arbitrary user-authored standard, so the user addresses it); info -> IMPROVE (one-line pitch); judgment -> JUDGMENT (surface for review). The message quotes the verbatim criterion statement + criterion id + source standards-file path. enforcement: mechanical criteria are NOT handled here -- audit.py enforces them under --config."
    special:
      procedure: "Surface the finding to the user with: the audit row that fired, the categories you attempted to match, the reasons none fit. The user proposes a strategy. If the strategy generalizes (mutually exclusive with A-J, recognizable detection signal, default remediation applies broadly), propose adding it as a new category in this taxonomy."
  enforcement:
    gate_kind: "audit-finding"
    gating_rule: "FAIL findings (categories A, C-class wrong-type with confirmed mismatch, D-mixed-type with confirmed drift, E-schema, H-back-reference, I-decision-provenance, L-load-graph orphan/dangling-edge FAILs, N-user-standard violation of a fail-severity criterion) gate compliance. The verdict is NON-COMPLIANT until all FAIL findings are resolved via the dispatched remediations. JUDGMENT findings do not gate compliance but are surfaced for review; INFO findings are advisory only."
    appeal_process: "JUDGMENT findings are resolved by user confirmation (treat as PASS once the user explicitly accepts the orientation-summary exception or the threshold-violation acceptance). FAIL findings have no bypass; remediation is always available within the taxonomy."
  gotchas:
    - "The corpus discovery (used by the roster and hierarchy procedures) and the cwd-relative discover.py (used by the audit procedure's index-based selection) are different tools. The corpus discovery walks User + Project + installed-Plugin roots; discover.py walks downward from cwd to enumerate nearby SKILL.md for the audit selector."
    - "The audit procedure operates on individual SKILL.md files even though the audit_skill's subject_type is corpus. The corpus is the addressable namespace (User + Project + Plugins); the audit procedure visits one file at a time within it. Roster and hierarchy use the same corpus subject."
    - "Decision rules for the audit verdict (FAIL -> NON-COMPLIANT; only PASS/INFO/JUDGMENT -> COMPLIANT) are unchanged across the type migration. The semantics are preserved; the YAML structure formalizes them."
    - "Roster and hierarchy procedures coexist with the audit procedure inside this audit-skill because they share the same corpus subject. They do NOT exercise the taxonomy + remediation dispatch (their output is inventory, not findings). The audit-skill contract permits supporting procedures that share the subject; only one procedure must exercise the dispatch machinery."
  anti_patterns:
    - id: "audit_then_self_remediate"
      name: "Audit and remediate in the same procedure pass"
      keywords: ["self-remediation", "single-pass", "verdict-and-fix"]
      why_it_seems_right: "It seems efficient to audit one file and apply remediations in the same pass -- one tool call, fewer round trips."
      why_it_is_wrong: "Mixing detection and remediation in one pass invalidates the idempotency contract. The verdict and the remediation are separate phases; conflating them prevents re-runs from producing the same findings and lets the agent silently mutate the subject without surfacing the change to the user."
      alternative: "Run the audit procedure to completion. Render the verdict. Dispatch remediations as separate FIX (auto-applied) and IMPROVE (opt-in Q&A) work units; surface SERIOUS at the top. Re-run the audit after remediation to verify."
    - id: "hygiene_as_verdict"
      name: "Treat hygiene thresholds as FAIL verdicts"
      keywords: ["hygiene threshold", "line count fail", "auto-split", "premature split"]
      why_it_seems_right: "The SKILL.md is over 500 lines / 3000 tokens; the threshold says so; surely that's a compliance failure?"
      why_it_is_wrong: "The threshold is a CRP-evaluation signal, not a verdict. Splitting a SKILL.md whose sections all serve the same reading task is a tool-call doubling, not a context-efficiency win. Auto-splitting on threshold alone destroys CRP."
      alternative: "Treat hygiene findings as INFO. Run the CRP test before proposing a split: do the body sections serve different reading tasks? Split only if yes."
```

## Argument grammar

Per-skill audit (the namesake procedure):

- `(none)` -- audit `<cwd>/SKILL.md` if present.
- `list` -- show numbered list of SKILL.md files under cwd; do not audit.
- `<path>` -- audit a specific SKILL.md.
- `<numbers>` -- audit files by index from the most recent `list` output (e.g. `3 7`).
- `fast` / `--fast` / `--yes` / `-y` -- non-interactive: skip the Q&A round and infer every IMPROVE/SPECIAL decision; FIX applies by definition, SERIOUS is surfaced. Combine with any selector, e.g. `/md-audit skill 3 7 fast`. Prose intent ("audit these and just apply everything, don't ask me") sets the same flag.
- `review` / `--review` -- review mode: audit a CHANGE rather than a file. Findings the change did not cause are suppressed, nothing is auto-applied (FIX is proposed, not applied), and the verdict is `DIFF-CLEAN` rather than `COMPLIANT`. For gating a submit / publish / handback. Combine with any selector, e.g. `/md-audit skill <path> --review`. Prose intent ("review my changes before I submit", "audit the diff") sets the same flag. Mutually exclusive with `fast` -- see Review mode. Off by default.

Corpus-wide reports:

- `roster` -- markdown roster to `<project-root>/tmp/skill-roster.md`.
- `roster <path>` -- markdown roster to `<path>`.
- `roster -` -- markdown roster body to stdout.
- `hierarchy` -- interactive HTML to `<project-root>/tmp/skill-hierarchy.html`.
- `hierarchy <path>` -- HTML to `<path>`.
- `hierarchy -` -- HTML body to stdout.

Typical workflows:

- *Audit one skill*: `/md-audit skill list` to see what's nearby, then `/md-audit skill 3` to audit by index.
- *Inventory all skills*: `/md-audit skill roster` -- scan the markdown to spot odd entries.
- *Browse with detail*: `/md-audit skill hierarchy` -- open the HTML to see every frontmatter key per skill.

## Workflow orchestration

The per-skill audit runs in two phases split by an interactive Q&A gate, and uses the Workflow tool to fan the work out across files. **Invoking this skill authorizes the Workflow-tool calls described below** (the skill's instructions are the opt-in; do not re-prompt the user for permission to orchestrate). Roster and hierarchy are script-based inventory and do not use the Workflow tool.

```
resolve (main loop)
  -> DETECT  (before-Q&A)  : 1 file inline | 2+ files via workflow/detect.js   -> structured findings
                             (review mode: ALWAYS via workflow/detect.js, any file count)
  -> render report (main loop)
  -> Q&A GATE (main loop)  : interactive decisions | inferred when non-interactive
  -> REMEDIATE (after-Q&A) : 1 file inline | 2+ files via workflow/remediate.js -> edits applied
  -> final summary + "re-run to verify"
```

**Multi-file threshold (the overhead equalizer).** The Workflow tool has real per-run overhead (background orchestration, agent spin-up). For a single file that overhead is not worth it, so a 1-file audit runs inline in the main loop. At 2+ files the parallel fan-out pays for itself, so detection (and, separately, remediation) go through the workflow scripts. Detection and remediation are **always separate passes** even in workflow mode -- the interactive Q&A sits between them, and a background workflow cannot ask the user anything. This split is also what preserves the `audit_then_self_remediate` anti-pattern: re-running the audit reproduces the same findings because nothing was remediated during detection.

**The two workflow scripts** (hand-authored, shipped as skill assets):

- `workflow/detect.js` -- before-Q&A. One lane per SKILL.md: run the mechanical validator -> apply the embedded CCP/CRP/ADP/decision-provenance recap -> classify. Returns `{ perFile, totals }`. No edits.
- `workflow/remediate.js` -- after-Q&A. One lane per file (disjoint skills, no conflicts): apply the decided edits (some touch a sibling `references/*.md` or the skill's co-located CLAUDE.md). Returns `{ perFile, summary }`.

Both accept `args` as an object or JSON string. Pass absolute `refs` paths (they run from the session cwd, not the skill dir).

## Non-interactive mode

When the non-interactive flag is set (argument token or expressed intent), the Q&A gate does not prompt. Instead, infer each IMPROVE/SPECIAL decision from the taxonomy's `default_remediation` plus the file content, apply them, and **list every inferred decision in the final summary** so the user can see and reverse them. FIX findings apply regardless; SERIOUS findings are surfaced summarized at the top and never auto-applied; SILENT findings are never surfaced. FAIL findings are still gated by the verdict; non-interactive only changes how the *decisions* are obtained, not the audit contract.

## Review mode

Normal mode audits a FILE. Review mode audits a CHANGE: same criteria, same lanes, but findings the change did not cause are suppressed and nothing is auto-applied. It exists to gate a submit / publish / handback, where a report full of pre-existing findings would either bloat the change with unrelated remediations or train the author to skim past the gate.

Three behavioral differences, and nothing else:

1. **Attributability filter.** Each finding is marked `attributable` by the lane, then the caller drops the ones the change did not cause. **SERIOUS always survives regardless** -- a secret or a violated invariant is not the author's doing and is still the most important thing on the page.
2. **Nothing is auto-applied.** FIX is demoted to a proposal at the Q&A gate. Mutually exclusive with `fast`, which would mean "propose instead of applying, but do not ask" -- i.e. nothing. Reject that combination rather than guessing.
3. **Verdict is `DIFF-CLEAN`, not `COMPLIANT`.** A weaker and more honest claim: *this change introduced no failure*, not *this file is clean*. A DIFF-CLEAN file may still carry a surviving SERIOUS.

Also: the multi-file threshold drops to 1 (always use the Workflow path), because a submit gate must not inherit whatever model the session happens to be running.

**You materialize the pre-images; the workflow never does.** This plugin is VCS-agnostic and must stay that way -- do not teach `detect.js` about Perforce or git. Before calling the workflow, write each SKILL.md's pre-change content to a temp path and pass it as `preImagePath`:

- Perforce: `p4 print -q -o <tmp> //depot/path/FILE#have`
- git: `git show <base>:<path> > <tmp>`, base = `merge-base(HEAD, origin/main)`, with the diff spanning `base..worktree` so committed-but-unpushed work is INSIDE the change under review rather than part of its baseline.

Infer which from the local repo. Two cases that are easy to get wrong:

- **Adds have no pre-image.** Pass `preImagePath: null` and every finding is attributable, which is correct -- the whole file is new. `p4 diff` emits nothing for an add, so detect adds via `p4 opened` rather than concluding the diff is unavailable.
- **Moves need the source.** `p4 print //new/path#have` fails for a `move/add`; resolve the pre-image through the move source.

**If you cannot obtain a pre-image, do not silently fall back to a whole-file audit.** Say the pre-image was unavailable and label the output as unfiltered, so nobody mistakes a normal audit for a change-scoped gate.

Two limits worth stating rather than hiding:

- **Only the SKILL.md gets a pre-image.** A skill's members (`references/*.md`, scripts, tests) are read at their CURRENT state in both passes, so a change confined to a reference doc -- an ADP back-reference it introduces, a member it orphans -- fires identically against the pre-image SKILL.md and reads as pre-existing. Findings whose cause lives outside the SKILL.md can escape the filter.
- **Attributability is judgment, not arithmetic.** It rests on re-detection, so a pre-existing finding the pre-image check happens to miss can resurface as attributable. Generous structural matching mitigates this; nothing eliminates it.

## Decision rules (per-skill audit verdict)

- Any FAIL finding -> file is NON-COMPLIANT.
- Only PASS, INFO, JUDGMENT findings -> file is COMPLIANT (with judgment-required calls noted).
- INFO findings are advisory improvements, not compliance failures.
- INFO findings do not escalate to FAIL on subsequent runs.
- **Review mode:** any *attributable* FAIL -> NON-COMPLIANT; otherwise DIFF-CLEAN. Non-attributable FAILs do not gate -- they predate the change -- but a non-attributable SERIOUS is still reported above the verdict.

## Cross-references

- Canonical audit framework (shared with `references-audit`): `skills-kit:md-audit/references/audit-framework.md` and `skills-kit:md-audit/references/audit-framework.yaml`.
- Canonical placement framework: `cohesion-principles (in skills-kit)`.
- Canonical type contracts: `framework.md (in skills-kit:skill-authoring)` and `plugins/skills-kit/skills_kit_lib/schema_registry.py`.
- Mechanical validator: `plugins/skills-kit/skills_kit_lib/audit.py` (run as `python -m skills_kit_lib.audit` from the plugin root).
- Type classifier (for category C remediation): `plugins/skills-kit/skills_kit_lib/classify.py` (run as `python -m skills_kit_lib.classify`).
- Shared corpus discovery: `plugins/skills-kit/skills_kit_lib/corpus.py` (used by the roster and hierarchy procedures).
- Report-script usage detail (roster / hierarchy discovery tiers, output formats, exit codes): `references/usage.md`.
- Sibling audit skill: `claude-md-audit` (via `/md-audit claude-md`) for CLAUDE.md files.
- Sibling reference scanner: `references-audit` (via `/md-audit references`) for broken skill cross-references across markdown.
