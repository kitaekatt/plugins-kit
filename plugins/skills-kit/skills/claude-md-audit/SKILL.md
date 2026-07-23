---
_schema_version: 1
name: claude-md-audit
author: christina
skill-type: audit-skill
description: Use when md-audit dispatches a CLAUDE.md audit against the cohesion framework; fans multi-file runs via the Workflow tool. Do NOT use for SKILL.md.
disable-model-invocation: true
user-invocable: false
argument-hint: "[file path, number(s) from list, or 'list'; add 'fast' for non-interactive]"
---

# CLAUDE.md Audit

## Plugin version (always echo first)

!`uv run python "${CLAUDE_PLUGIN_ROOT}/scripts/print_version.py"`

The first line of your response MUST be the `Running ...` line printed above. This gives the user immediate confirmation of which plugin version actually executed (the slash registry can lag the on-disk cache; this is the only reliable signal).

Audit a CLAUDE.md (root, ancestor, child, or `.local`) against the cohesion-principles content-allocation framework. Findings are grouped by principle: CCP (write-together / change cadence), CRP (read-together / smallest correct scope), ADP (link-forward-only / DAG), plus universal hygiene rules and optional schema validation when a `claude_md:` YAML contract block is present.

The audit is idempotent: same input produces the same findings; addressing all FAIL findings produces a COMPLIANT verdict on the next run.

```yaml
audit_skill:
  _schema_version: "1"
  identity: "Audit CLAUDE.md files against the cohesion-principles content-allocation framework (CCP / CRP / ADP), plus universal hygiene and optional claude_md schema validation. Classify findings into a taxonomy and dispatch remediations by bucket."
  scope:
    covers:
      - "auditing a CLAUDE.md or CLAUDE.local.md against CCP / CRP / ADP placement rules (judgment-based from cohesion-principles)"
      - "applying the role-to-criteria map (root / ancestor / child / local roles have different applicable rules)"
      - "schema validation when a `claude_md:` YAML contract block is present in the file"
      - "the code-directory insight-validation dimension (CD-1..CD-6) for files flagged `dimension: code-directory` by discover.py -- anchor-modality classification, fidelity-to-code, and the what-we-care-about value filter (self-contained in references/code-dir-insight-filter.md)"
      - "the opt-in density lens (DD-1..DD-4) -- verbosity-in-place, extract-to-reference, intra-file redundancy, value-earns-tokens; advisory only (JUDGMENT, disposition IMPROVE, never FAIL); self-contained in references/density-criteria.md; runs only when the `density` argument or equivalent intent is given"
      - "classifying each finding into one of four dispositions instance-level (FIX auto-applied / SERIOUS surfaced-at-top / IMPROVE count-plus-one-liners / SILENT not-surfaced; K -> SPECIAL) per the detect.js classifier"
      - "listing CLAUDE.md files visible from cwd (the cwd-relative discover.py helper for index-based selection)"
    excludes:
      - "auditing SKILL.md files (use /md-audit skill)"
      - "auditing reference docs (audited transitively via the SKILL.md they belong to)"
      - "auditing cross-references between skills or docs (use /md-audit references)"
  subject:
    what: "Claude Code CLAUDE.md / CLAUDE.local.md files (root, ancestor, child, or local roles), evaluated against the cohesion-principles content-allocation framework."
    subject_type: "corpus"
  criteria:
    - id: "ccp_change_cadence"
      name: "CCP -- content changes for the same reason"
      keywords: ["ccp", "change cadence", "single reason", "content allocation"]
      summary: "Each rule, insight, or convention in a CLAUDE.md belongs to that file only when it changes for the same reason as the file's role (project conventions for project-root CLAUDE.md, directory-local invariants for child CLAUDE.md, etc.)."
      severity: "JUDGMENT"
      detail: "Judgment call per cohesion-principles per_artifact_role.claude_md.audit_rules. The agent reads the body and asks: does this content's change cadence match the file's role?"
    - id: "ccp_cross_file_duplication"
      name: "CCP -- no cross-file rule duplication along the role chain"
      keywords: ["ccp", "duplication", "parent rule", "ancestor inheritance"]
      summary: "A rule stated in a parent CLAUDE.md (ancestor role) must not be restated in a child CLAUDE.md. The agent loads the parent automatically when descending into the child."
      severity: "FAIL"
      detail: "Detected by reading the parent CLAUDE.md (when available) and comparing rule statements. Restated rules signal a misunderstanding of the load model."
    - id: "crp_size_signal"
      name: "CRP -- body size as an evaluation prompt"
      keywords: ["crp", "size threshold", "split signal", "progressive disclosure"]
      summary: "A CLAUDE.md over the size threshold (500 lines / 3000 tokens approx) is a signal to evaluate whether sections serve different reading tasks; the threshold itself is not a verdict."
      severity: "INFO"
      detail: "Mechanical line/token count. Triggers a CRP-evaluation prompt; the agent runs the test (do sections serve different reading tasks?) before proposing a split."
    - id: "crp_role_appropriate"
      name: "CRP -- content sits at the role with the smallest correct scope"
      keywords: ["crp", "role scope", "smallest correct scope", "wrong role"]
      summary: "A rule that applies only to a subdirectory belongs in that subdirectory's CLAUDE.md, not the project root. A rule that applies everywhere belongs in the root, not duplicated per subdirectory."
      severity: "JUDGMENT"
      detail: "Judgment call from cohesion-principles. The agent asks: what is the smallest scope where this rule is correct? Place it there."
    - id: "adp_no_forward_dependency"
      name: "ADP -- no forward dependency on descendant CLAUDE.md content"
      keywords: ["adp", "forward dependency", "dag", "descendant reference"]
      summary: "Parent (root or ancestor) CLAUDE.md must not depend on or reference descendant CLAUDE.md content. The load graph flows root -> ancestor -> child, one direction."
      severity: "FAIL"
      detail: "Detected by scanning the body for descendant-path references or load-time assumptions about subdir CLAUDE.md content."
    - id: "hygiene_thresholds"
      name: "Hygiene -- universal field and length rules"
      keywords: ["hygiene", "line count", "token count", "structural rules"]
      summary: "Body length, broken markdown links, and other universal structural rules. Most are INFO severity unless they cross a hard threshold."
      severity: "INFO"
      detail: "Mechanical universal rules. Distinct from CRP -- hygiene checks structural correctness; CRP checks placement intent."
    - id: "schema_validation"
      name: "claude_md: YAML block validates against schema (when present)"
      keywords: ["claude_md schema", "yaml validation", "optional contract", "claude-md schema"]
      summary: "Files carrying a `claude_md:` YAML contract block in the body must validate against CLAUDE_MD_SCHEMA in schemas.py. Files without the block are not gated on schema validation."
      severity: "FAIL"
      detail: "Mechanical validation via audit.py when the block is present. Conditional: applies only when the file declares the contract. Root-role files SHOULD carry the block (claude-md-authoring adds one when it touches a root file); absence on a pre-existing root file is surfaced as INFO, never FAIL."
    - id: "cd_anchor_modality_classify"
      name: "CodeDir -- classify every anchor's modality before any existence check"
      keywords: ["code-directory", "anchor modality", "requires-present", "requires-absent", "external", "template", "vendored", "generated"]
      summary: "For a code-directory file, tag each concrete anchor (symbol / file / sibling / field / name) with exactly one modality FIRST. Only `requires-present` is eligible for FAIL; `requires-absent` scores inverted; external / template-or-env / vendored / generated-or-unsynced / non-anchor never FAIL."
      severity: "JUDGMENT"
      detail: "Precondition for cd_fidelity. The Level-2 safety valve: because the Level-1 trigger fires generously, modality classification is what prevents false FAILs on negative-existence, external, templated, and generated anchors. Full table in references/code-dir-insight-filter.md."
    - id: "cd_fidelity_anchor_resolves"
      name: "CodeDir -- claim anchor resolves (or, for requires-absent, stays absent)"
      keywords: ["code-directory", "fidelity", "stale anchor", "anchor resolves", "inverted absence"]
      summary: "A `requires-present` anchor that is named-and-absent after a repo-wide check is a FAIL (H_stale_anchor). A `requires-absent` anchor whose asserted-absent thing is now present is a FAIL (H2_inverted_absence -- the invariant is violated). All other modalities are PASS/INFO."
      severity: "FAIL"
      detail: "Mechanical resolution: symbols repo-wide, leading-slash paths against repo root. Conditional on dimension=code-directory. This is the only CD criterion that gates compliance."
    - id: "cd_fidelity_line_anchor"
      name: "CodeDir -- cited line number tracks its symbol"
      keywords: ["code-directory", "line drift", "line anchor", "re-anchor", "symbol coupled"]
      summary: "When a claim cites a line number, find the enclosing symbol it names; if the symbol resolves but is >~30 lines from the cited number, flag I2_line_drift (drop the number, keep the symbol). Stay silent if the author supplied a recovery hint."
      severity: "JUDGMENT"
      detail: "Coupled to symbol resolution; never fires when no line number is cited. Disposition FIX (the remediation is the mechanical removal of the stale number -- a convention-violation fix)."
    - id: "cd_fidelity_claim_holds"
      name: "CodeDir -- claim still matches the code in kind"
      keywords: ["code-directory", "claim drift", "stale claim", "in kind", "counted magnitude"]
      summary: "Read the anchored code; if the claim no longer holds in kind (god-object now decomposed, TODO now resolved, bypass now gone) flag I_claim_drift. Counted magnitudes ('7200-line', '12 files') are intentionally fuzzy -- never FAIL on the number; flag only on kind-inversion."
      severity: "JUDGMENT"
      detail: "Never auto-FAIL. Disposition FIX when the audit verified the actual behavior from the code reading (the code reading is evidence; intent re-derivation is not a blocker); IMPROVE only when no fact decides the correct claim."
    - id: "cd_value_insight_earns_place"
      name: "CodeDir -- section earns its place under the what-we-care-about filter"
      keywords: ["code-directory", "value filter", "earns place", "low value", "inventory", "carve-out"]
      summary: "Each section must pass the value lattice (silent-failure > blast-radius > deliberately-wrong > safety > perf > ownership). Linter-caught / default / bare-inventory / pure-restatement sections are low-value (J). Honor every carve-out: annotated Files/Schema blocks, SSOT-pointing catalogs, safety-rail cheatsheets, topology tables are NOT low-value."
      severity: "JUDGMENT"
      detail: "Disposition FIX for a bare un-annotated inventory / default / restatement (delete, be aggressive); IMPROVE for a trim of true content passing the one-line test; SILENT for a validator artifact or accepted structural pattern. Carve-outs are load-bearing -- both maintainer-agents required them."
    - id: "cd_silent_failure_preserved"
      name: "CodeDir -- highest-value content still present"
      keywords: ["code-directory", "silent failure preserved", "erosion signal", "value erosion"]
      summary: "Positive check: if the file has been reduced to only structural description with no tier-1/tier-2 silent-failure or blast-radius claim, emit an erosion INFO -- the highest-value content may have been edited out."
      severity: "INFO"
      detail: "Advisory only; never gates. Surfaces value erosion across edits."
    - id: "dd_density_in_place"
      name: "Density -- correctly-placed, valuable section is over-worded"
      keywords: ["density", "verbosity", "tighten", "ceremony", "over-explanation", "token reduction"]
      summary: "A section that is correctly placed and carries real value but says in N words what materially fewer would carry (redundant restatement, hedging/ceremony preamble, over-explanation of the obvious). Remediation tightens IN PLACE; never moves or deletes. Honors carve-outs (load-bearing nuance, teaching examples, labeled safety-rail repetition)."
      severity: "JUDGMENT"
      detail: "Opt-in density lens, loaded only on the `density` request. Advisory: never FAIL. Self-contained in references/density-criteria.md (DD-1). Disposition IMPROVE; remediation must route tokens (tighten in place)."
    - id: "dd_extract_to_reference"
      name: "Density -- self-contained block should be disclosed to a reference"
      keywords: ["density", "disclosure", "extract to reference", "progressive disclosure", "L1 to L3", "on-demand block"]
      summary: "A self-contained block serving an on-demand/narrow reading task, large enough that inlining taxes every reader, should move to a references/*.md (or SKILL.md for on-task procedure) leaving a one-line pointer. Disclosure-level move, not scope-level -- distinct from crp_role_appropriate (wrong file) and finer than C_crp_split_candidate (whole-file split)."
      severity: "JUDGMENT"
      detail: "Opt-in density lens (DD-2). Advisory: never FAIL. Disposition IMPROVE; remediation names the destination reference and the pointer left behind."
    - id: "dd_intra_file_redundancy"
      name: "Density -- same fact stated more than once within one file"
      keywords: ["density", "intra-file duplication", "redundancy", "state once", "cross-reference"]
      summary: "The same fact stated multiple times within THIS file (distinct from ccp_cross_file_duplication, which is across the role chain and FAIL/FIX). Keep the single best statement; replace the others with a cross-reference."
      severity: "JUDGMENT"
      detail: "Opt-in density lens (DD-3). Advisory: never FAIL. Disposition IMPROVE; remediation names which statement survives."
    - id: "dd_value_earns_tokens"
      name: "Density -- section does not earn its tokens (classic-file value filter)"
      keywords: ["density", "value filter", "earns tokens", "low value verbose", "downgrade"]
      summary: "The classic-file generalization of the code-directory value filter: a section that does not earn its tokens under the value lattice AND is verbose about it. Defers the ranking + carve-outs to code-dir-insight-filter.md Step 4 (SSOT). Does NOT double-count with CD-5/J -- on a code-directory file, value findings stay in CD-5."
      severity: "JUDGMENT"
      detail: "Opt-in density lens (DD-4). Advisory: never FAIL. Disposition IMPROVE; remediation proposes downgrade-to-a-line or confirmed deletion of a contentless section."
  taxonomy:
    - id: "A_wrong_role_content"
      name: "Content sits at the wrong role in the CLAUDE.md hierarchy"
      keywords: ["wrong role", "wrong scope", "child rule in root", "root rule in child"]
      detection_signal: "Agent judgment from cohesion-principles role-to-criteria map. Body section's scope is narrower or broader than the file's role allows."
      default_remediation: "Propose moving the section to the correct-scope CLAUDE.md (e.g. narrow root rule -> subdirectory CLAUDE.md; broad subdir rule -> project root CLAUDE.md). User confirms the move."
      bucket: "IMPROVE"
    - id: "B_ccp_cross_file_duplication"
      name: "Rule restated from parent CLAUDE.md"
      keywords: ["duplication", "parent rule", "inheritance violation", "redundant"]
      detection_signal: "Body restates a rule already present in an ancestor CLAUDE.md (read during the audit's role-walk phase)."
      default_remediation: "Delete the restated rule from the child file (the parent rule loads automatically when the agent descends into the child). Apply the loss-free-deletion guard first: diff the restated rule against the parent copy and fold any child-local delta into the parent SSOT (or a REMINDER-PLUS-REFERENCE summary line of a dozen tokens or less naming it) before deleting."
      bucket: "FIX"
    - id: "C_crp_split_candidate"
      name: "Body sections serve different reading tasks (CRP split warranted)"
      keywords: ["crp split", "different reading tasks", "progressive disclosure", "decomposition"]
      detection_signal: "Body over size threshold AND agent judgment that sections genuinely serve different reading tasks (e.g. setup-time rules + on-task triggers + reference glossary)."
      default_remediation: "Propose an L1 -> L2 / L3 decomposition: move on-task content to a SKILL.md (L2); move reference content to a reference doc (L3). User confirms before splitting."
      bucket: "IMPROVE"
    - id: "D_adp_forward_dependency"
      name: "Parent CLAUDE.md depends on descendant content"
      keywords: ["adp", "forward dependency", "graph cycle", "wrong load order"]
      detection_signal: "Body references or assumes content from a descendant CLAUDE.md (e.g. 'see subsystem/CLAUDE.md for the rule')."
      default_remediation: "Either inline the descendant content into the parent (if the rule is truly parent-scoped) or remove the forward reference (if the rule is descendant-scoped and the parent has no business assuming it). User confirms."
      bucket: "IMPROVE"
    - id: "E_schema_failure"
      name: "claude_md: YAML block fails schema validation"
      keywords: ["schema fail", "claude_md schema", "yaml validation", "contract block"]
      detection_signal: "audit.py reports schema validation failure for the file's claude_md: YAML block (missing required key, wrong type, forbidden key)."
      default_remediation: "Surface the failing rows. A missing field with a sensible default is decidable by convention -> FIX (add it). A field requiring authorial judgment -> IMPROVE (offer it as a one-liner)."
      bucket: "FIX"
    - id: "F_hygiene_threshold"
      name: "Body over size threshold (CRP-evaluation prompt)"
      keywords: ["hygiene", "size threshold", "line count", "token count"]
      detection_signal: "Mechanical INFO finding: body line count > 500 or token count > 3000."
      default_remediation: "Run the CRP test (do sections serve different reading tasks?). If yes, escalate to C. If no, INFO stays as-is."
      bucket: "IMPROVE"
    - id: "G_descendant_role_mismatch"
      name: "Local file (.local) carries non-local content"
      keywords: [".local", "personal scope", "machine-specific", "wrong file"]
      detection_signal: "CLAUDE.local.md body contains project-conventional content that should be in the checked-in CLAUDE.md instead of a personal override."
      default_remediation: "Propose moving the project-conventional content to the checked-in CLAUDE.md (so all collaborators see it). User confirms before moving."
      bucket: "IMPROVE"
    - id: "P_stale_factual_claim"
      name: "A numeric count or checkable factual claim is contradicted by current repo state"
      keywords: ["stale count", "stale claim", "factual drift", "wrong number", "A-3 classic home"]
      detection_signal: "A-3 stale-reference hit: a numeric count or other checkable factual claim in a classic (non-code-directory) CLAUDE.md (e.g. 'the six unittest suites') is contradicted by current repo state (e.g. seven test files exist)."
      default_remediation: "FIX when the fix is a mechanical count/value update with unambiguous ground truth (recount and correct the number; prefer the information-preserving fix -- correct the count AND add the missing entry). IMPROVE when the discrepancy might be intentional (the count is aspirational or the claim is ambiguous) -- offer it as a one-liner."
      bucket: "FIX"
    - id: "Q_skill_content_duplication"
      name: "CLAUDE.md restates content a skill owns"
      keywords: ["skill duplication", "C-6", "skill content in CLAUDE.md", "trim to guardrail", "pointer"]
      detection_signal: "C-6 hit: a substantial block in a CLAUDE.md (or a project reference doc it cites) restates content owned by a skill or skill reference (verbatim or near-verbatim). NOT B, which is ancestor-CLAUDE.md restatement."
      default_remediation: "Trim to a one-line guardrail naming the rule or failure mode plus a pointer to the skill (per C-5/A-4); the skill carries the depth. This is dedup under the summarize-and-reference rule (REMINDER PLUS REFERENCE): keep an inline reminder of a dozen tokens or less plus the pointer to the SSOT skill, reference-only beyond that budget. Apply the loss-free-deletion guard first -- fold any local delta into the guardrail line before trimming."
      bucket: "FIX"
    - id: "R_ancestor_convention_violation"
      name: "Subject violates a convention an ancestor CLAUDE.md explicitly declares (H-11)"
      keywords: ["ancestor convention", "H-11", "ascii-only", "no absolute paths", "declared convention", "verbatim rule"]
      detection_signal: "H-11 hit: the subject violates a convention EXPLICITLY declared in an ancestor CLAUDE.md (loaded ambient), and the exact declared rule is quotable VERBATIM from that ancestor (no inferred / generic conventions). Fires only when ancestorClaudeMdPaths is supplied and non-empty; a root file with no ancestors emits nothing."
      default_remediation: "FIX for a mechanical correction against the documented convention (replace a non-ASCII look-alike, relativize a hardcoded absolute path, apply the stated formatting rule); the message carries the verbatim ancestor rule quote + the ancestor source path. SERIOUS when the violation reveals a real-world problem the rule exists to prevent (e.g. a committed secret an ancestor forbids) -- surfaced at the top, never auto-fixed."
      bucket: "FIX"
    - id: "H_stale_anchor"
      name: "CodeDir: requires-present anchor no longer resolves"
      keywords: ["code-directory", "stale anchor", "broken symbol", "missing sibling", "fidelity"]
      detection_signal: "A `requires-present` anchor (symbol / file / sibling / field the claim says should exist) is absent after a repo-wide check, and is not classified external / generated / template / vendored."
      default_remediation: "Re-anchor the claim to the current symbol/path (FIX -- the target mechanism was found), or delete the claim if the code it describes is gone (FIX -- deleting falsified content loses nothing). SERIOUS instead when the stale anchor is a protective rail with NO surviving mechanism -- the real finding is the unprotected invariant."
      bucket: "FIX"
    - id: "H2_inverted_absence"
      name: "CodeDir: requires-absent thing is now present"
      keywords: ["code-directory", "negative existence", "tracked secret", "forbidden present", "invariant violated"]
      detection_signal: "A `requires-absent` claim's asserted-absent thing now exists (a tracked file under a gitignored SSOT path; a FORBIDDEN name that now resolves)."
      default_remediation: "Surface loudly at the TOP of the report as a SERIOUS finding -- the invariant the claim guards is violated; the doc problem reveals a real-world problem. The fix is in the code/repo, not the CLAUDE.md. Never auto-fixed."
      bucket: "SERIOUS"
    - id: "I_claim_drift"
      name: "CodeDir: claim no longer matches the code in kind"
      keywords: ["code-directory", "claim drift", "stale claim", "in kind"]
      detection_signal: "Reading the anchored code contradicts the claim in kind (decomposed god-object, resolved TODO, bypass now gone). NOT a counted-magnitude difference."
      default_remediation: "Update the mechanism/magnitude or retire the claim. FIX when the audit verified the actual behavior from the code reading -- intent re-derivation is not a blocker, the code reading is evidence. IMPROVE only when the correct claim cannot be decided from a fact (offer as a one-liner)."
      bucket: "FIX"
    - id: "I2_line_drift"
      name: "CodeDir: cited line number drifted from its symbol"
      keywords: ["code-directory", "line drift", "re-anchor", "drop line number"]
      detection_signal: "The enclosing symbol the claim names resolves but is >~30 lines from the cited number, and the author gave no recovery hint."
      default_remediation: "Drop the line number; keep the symbol anchor. Mechanical convention-violation fix."
      bucket: "FIX"
    - id: "J_low_value_insight"
      name: "CodeDir: section fails the what-we-care-about value filter"
      keywords: ["code-directory", "low value", "bare inventory", "restatement", "value filter"]
      detection_signal: "A section is linter-caught / a language default / a bare un-annotated inventory / a pure schema restatement -- AND not protected by a carve-out (annotated Files/Schema, SSOT-pointing catalog, safety-rail cheatsheet, topology table)."
      default_remediation: "FIX for a bare un-annotated inventory / language default / pure restatement / linter-caught content -- delete it (be aggressive; a default trim is decidable). IMPROVE when the section carries TRUE content that passes the one-line test (offer the trim as a one-liner). SILENT for a validator detection artifact or an accepted structural pattern (historical record, agent-definition file). Apply the loss-free-deletion guard before any deletion."
      bucket: "FIX"
    - id: "L_verbose_in_place"
      name: "Density: correctly-placed section is over-worded"
      keywords: ["density", "verbose", "tighten in place", "ceremony", "token reduction"]
      detection_signal: "Density lens (DD-1): a valuable, correctly-scoped section uses materially more words than its information content requires, and is not protected by a carve-out (teaching example, load-bearing nuance, labeled safety-rail repetition)."
      default_remediation: "Propose a tightened rewrite (or the specific sentences to compress) IN PLACE, with an approximate token-savings figure. Never move or delete. Offer as a one-liner (trim of true content passing the one-line test)."
      bucket: "IMPROVE"
    - id: "M_extract_to_reference"
      name: "Density: self-contained block should move to a reference"
      keywords: ["density", "disclosure", "extract to reference", "pointer", "progressive disclosure"]
      detection_signal: "Density lens (DD-2): a self-contained on-demand block is large enough to tax every reader who does not need it; it belongs one disclosure level deeper (references/*.md or a SKILL.md) within the same scope."
      default_remediation: "Propose moving the block to a named reference doc and leaving a one-line pointer behind, with an approximate token-savings figure. A structural (disclosure-level) move -- offer as a one-liner."
      bucket: "IMPROVE"
    - id: "N_intra_file_redundancy"
      name: "Density: same fact stated more than once within one file"
      keywords: ["density", "intra-file duplication", "redundancy", "state once"]
      detection_signal: "Density lens (DD-3): a fact is restated in multiple sections of THIS file (not across the role chain -- that is B)."
      default_remediation: "Propose keeping the single best statement and replacing the others with a cross-reference (the summarize-and-reference rule, within one file). Offer as a one-liner; the density lens stays advisory."
      bucket: "IMPROVE"
    - id: "O_low_value_verbose"
      name: "Density: section does not earn its tokens (classic-file value filter)"
      keywords: ["density", "low value", "value filter", "downgrade", "earns tokens"]
      detection_signal: "Density lens (DD-4): a classic-file section fails the value lattice (code-dir-insight-filter.md Step 4) AND is verbose. Not run on code-directory files (CD-5/J owns value there)."
      default_remediation: "Propose downgrade (compress to a line) or, for a contentless section, deletion. Offer as a one-liner; this opt-in lens stays IMPROVE and never auto-deletes."
      bucket: "IMPROVE"
    - id: "K_unclassified"
      name: "Unclassified / special case"
      keywords: ["unclassified", "special case", "escape hatch", "K bucket"]
      detection_signal: "Finding does not match any A-G, P, Q, or H-J detection signal after deliberate attempt."
      default_remediation: "Surface to the user with the audit row that fired, attempted matches, and reasons none fit. User proposes strategy."
      bucket: "SPECIAL"
    - id: "N_user_standard_violation"
      name: "CLAUDE.md violates a user-authored standards criterion (standards_set)"
      keywords: ["user standard", "standards_set", "configurable standard", "verbatim criterion", "layered standards", "user opinion"]
      detection_signal: "A criterion from a resolved *-standards.md (standards_set) governing the claude_md primitive is violated, with the criterion statement quotable VERBATIM from the standards file. Judgment criteria only (enforcement judgment or absent); enforcement: mechanical criteria are audit.py's job under --config, not the lane's. Fires only when standardsPaths is supplied and non-empty. Suppressed when the criterion id is in disabledCriteria. (The N_ letter is shared verbatim across all three md-audit members for this cross-cutting user-standards category; it is distinct from N_intra_file_redundancy in this member's own taxonomy -- the suffix disambiguates.)"
      default_remediation: "Disposition follows the criterion's declared severity: fail -> SERIOUS (a hard user-declared rule the auditor cannot mechanically satisfy -- surfaced at the top, never auto-fixed; the message carries the verbatim statement + criterion id + source standards-file path); info -> IMPROVE (one-line pitch); judgment -> JUDGMENT (surfaced for review). An arbitrary user standard is not mechanically fixable, so N is never auto-applied."
      bucket: "SERIOUS"
  procedures:
    - id: "audit_claude_md"
      name: "Audit one CLAUDE.md and dispatch remediations"
      keywords: ["audit", "claude.md", "single-file audit", "compliance verdict", "dispatch"]
      goal: "For each target CLAUDE.md, run mechanical and judgment-based checks against the framework's contract, classify findings into the taxonomy, assign each a disposition (FIX / SERIOUS / IMPROVE / SILENT; K -> SPECIAL), and emit a per-file compliance verdict."
      preconditions:
        - "audit.py is reachable (mechanical schema validator -- only needed if a claude_md: YAML block is present)."
        - "references/audit-criteria.md is loadable (the self-contained classic criteria doc; the upstream cohesion-principles is its derivation and is NOT loaded by the audit path)."
        - "references/code-dir-insight-filter.md is loadable -- needed only when a target is flagged dimension=code-directory; the self-contained CD-1..CD-6 criteria, anchor-modality table, and value filter."
        - "references/density-criteria.md is loadable -- needed only when the density lens was requested (the `density` arg or equivalent intent); the self-contained DD-1..DD-4 criteria and the density-not-deletion rule."
        - "The user is in a project directory so role classification works."
      steps:
        - n: 1
          action: "Resolve the audit target set from $ARGUMENTS. Empty -> cwd/CLAUDE.md. 'list' -> emit numbered list via discover.py and stop. Integers -> map to paths from last list. Path -> use directly. Strip any non-interactive token ('fast', '--fast', '--yes', '-y') from the args first and set non_interactive accordingly (also set it if the user's prose expresses non-interactive intent, e.g. 'just apply everything, don't ask'). Strip the review token ('review', '--review') and set review=true (also set it if the user's prose expresses the intent, e.g. 'review my changes before I submit', 'audit the diff'); review is FALSE by default. Reject the combination review + non_interactive: 'propose instead of applying, but do not ask' resolves to doing nothing -- tell the user the two are mutually exclusive and stop. Strip the density-lens token ('density', '--density') and set density_lens=true (also set it if the user's prose expresses the intent, e.g. 'is this too verbose', 'can anything move to a reference', 'audit for token efficiency'); density_lens is FALSE by default and the lens never runs unless requested. For each target capture (path, role, dimension, parentPath, ancestorClaudeMdPaths) where role is root / ancestor / child / local, dimension is the `code-directory`|`classic` flag discover.py emits (Level-1 trigger), and parentPath is the nearest ancestor CLAUDE.md for a child (else null). ancestorClaudeMdPaths (for the H-11 ancestor-convention check) is the FULL ancestor CLAUDE.md chain above the subject, enumerated deterministically: starting from the subject's PARENT directory, walk up one directory at a time until (and including) the workspace root -- the cwd when there is no enclosing project, otherwise the nearest ancestor containing a `.git` entry -- and for each directory Glob/stat `<dir>/CLAUDE.md`; collect every one that exists into a list ordered NEAREST-ANCESTOR FIRST, EXCLUDING the subject file itself. This list deliberately INCLUDES the file parentPath names (they overlap on the nearest ancestor and drive different criteria -- parentPath -> B duplication, ancestorClaudeMdPaths -> H-11 conventions -- so no dedup is needed). Empty for a root file with nothing above it. ALSO, ONCE per run (not per file), resolve the configurable standards via the plugin venv: (cd ${CLAUDE_PLUGIN_ROOT} && <venvPython> scripts/resolve_standards.py --project-root <workspace root> --primitive claude_md), and parse its JSON { disabled, thresholds, standards }. Keep a run-level `disabledCriteria` = the `disabled` array (the optional rule/criterion ids the user switched off) and, for every target CLAUDE.md, `standardsPaths` = the `standards.claude_md` array (the user-authored *-standards.md files governing this member's primitive). Both thread into the detect step. An empty or absent config yields empty lists, so the default behavior is unchanged."
          tool: "discover.py"
          input: "uv run python ${CLAUDE_PLUGIN_ROOT}/skills/claude-md-audit/scripts/discover.py [--json]"
          expected: "Resolved (path, role, dimension, parentPath) tuples + non_interactive flag."
          on_failure: "If no CLAUDE.md resolves, surface cwd and stop. If a path is given directly (not via discover.py), classify its dimension by reading scripts/discover.py::classify_dimension semantics or default to `code-directory` when the file has code/yaml/csv siblings or review-claim markers and no `claude_md:` block."
        - n: 2
          action: "DETECT phase (before-Q&A). Choose execution mode by file count -- this threshold equalizes the Workflow tool's per-run overhead. REVIEW MODE OVERRIDE: when review is TRUE the threshold is 1, so ALWAYS use the Workflow path even for a single file. Review mode gates a submit/publish, so its verdict must not depend on whatever model the session happens to be running; only the lane pins model+effort and enforces the schema. Never run a review-mode detect inline. ONE file (non-review): audit inline in the main loop (read the file + its parent if child; Read references/audit-criteria.md -- the single self-contained criteria doc, which states each testable rule with its CCP/CRP/ADP derivation inline; do NOT also load cohesion-principles; apply the role-to-criteria map; if ancestorClaudeMdPaths is non-empty run the H-11 ancestor-convention check inline exactly as the lane does -- read each ancestor CLAUDE.md, flag a subject violation ONLY when the declared rule can be quoted VERBATIM from the ancestor (no inferred/generic conventions), emit it as group Hygiene, taxonomy R_ancestor_convention_violation, FAIL, with the verbatim quote + ancestor source path in the message; exception awareness (applies to H-11 AND to the built-in non-ASCII / hardcoded-absolute-path convention FIX in the classifier): when an ancestor EXPLICITLY declares a scoped exception that covers the specific instance -- right file scope AND right content kind, e.g. 'ASCII only, except developer names in the contributors section may contain non-ASCII characters' -- do NOT flag that instance under either check; demote it to PASS/INFO and cite the verbatim exception quote + ancestor source path in the message. The one declared rule + exception governs both checks so they never contradict (H-11 silent while the built-in convention FIX still fires is exactly the bug this removes); no inferred or stretched exceptions, and when in doubt the check STILL fires. if standardsPaths is non-empty apply the user-authored standards inline exactly as the lane does (read each standards file, apply ONLY criteria quotable VERBATIM, SKIP enforcement: mechanical criteria -- audit.py --config owns those, emit each violation as group Hygiene, taxonomy N_user_standard_violation, severity from the criterion's declared severity (fail->FAIL, info->INFO, judgment->JUDGMENT), message carrying the verbatim statement + criterion id + source path); suppress any finding whose criterion/rule id is in disabledCriteria (never an architectural or integrity id); If a `claude_md:` block is present run the schema validator (now with --config so audit.py drops disabled mechanical rows and overlays thresholds); if dimension=code-directory ALSO Read references/code-dir-insight-filter.md and run the CD-1..CD-6 insight-validation dimension -- classify each anchor's modality first, then fidelity/line/claim/value; for dimension=classic do NOT load the filter; if density_lens is TRUE ALSO Read references/density-criteria.md and run the DD-1..DD-4 density lens -- emit findings under group Density, all JUDGMENT/DISCUSS, never FAIL/AUTO, each remediation naming where the tokens go; if density_lens is FALSE do NOT load that doc; classify each finding into taxonomy + bucket). TWO OR MORE files (or ANY count in review mode): call the Workflow tool with scriptPath ${CLAUDE_PLUGIN_ROOT}/skills/claude-md-audit/workflow/detect.js and args = { files:[{path,role,dimension,parentPath,ancestorClaudeMdPaths,standardsPaths,preImagePath,parentPreImagePath}], disabledCriteria:<run-level disabled id list>, density:<density_lens bool>, review:<review bool>, refs:{criteria, codeDirFilter, densityCriteria, pluginRoot, venvPython} }. The workflow fans one lane out per file and returns { perFile:[...], totals, review }. In review mode YOU materialize each pre-image BEFORE calling the workflow (see the review-mode section) and pass its path; the workflow is VCS-agnostic and will not fetch anything itself. Detection only -- no file is edited in this phase."
          tool: "Workflow | inline"
          input: "detect.js args.refs: criteria=${CLAUDE_PLUGIN_ROOT}/skills/claude-md-audit/references/audit-criteria.md; codeDirFilter=${CLAUDE_PLUGIN_ROOT}/skills/claude-md-audit/references/code-dir-insight-filter.md; densityCriteria=${CLAUDE_PLUGIN_ROOT}/skills/claude-md-audit/references/density-criteria.md (passed only when args.density is true); pluginRoot=${CLAUDE_PLUGIN_ROOT}; venvPython=<plugin venv python>. Each file carries its dimension; the lane loads the code-dir filter only for dimension=code-directory and the density criteria only when args.density is true. (cohesion-principles is intentionally NOT passed -- lanes load only the self-contained criteria doc(s) for cache efficiency.) Schema validator is run as: (cd ${CLAUDE_PLUGIN_ROOT} && <venvPython> -m skills_kit_lib.audit <path> --json --config) -- --config makes audit.py honor the resolved config (drop disabled mechanical rows, overlay thresholds)."
          expected: "Structured per-file findings (group incl. CodeDir and Density, severity, criterion, message, line, taxonomy, bucket, remediation) + per-file verdict."
          on_failure: "If the Workflow tool is not available in this environment (subagent contexts do not expose it), fall back to the ONE-file inline detect procedure run sequentially per file -- detection and remediation stay separate passes. EXCEPT in review mode, where this fallback does not apply: inline detection inherits the session model and forfeits the pin the gate depends on, so either stop and tell the user review mode needs a main-session run, or run inline and label the result advisory-and-unpinned rather than a passed gate. If the schema validator is unavailable, the lane marks the Schema group JUDGMENT ('validator unavailable') and continues -- never fail a file for that. If an anchor cannot be classified or resolved cheaply, mark it external-unverifiable/INFO rather than FAIL."
        - n: 3
          action: "Render the per-file report (output_template) from the collected findings, then the REPORT CONTRACT summary in three visible sections IN THIS ORDER, no hedging: (1) SERIOUS -- lead with 'Found <N> serious issue(s) that require fixing' and a one-line summary per issue (secrets, fictional protective rails, doc problems that reveal a real-world problem); never auto-fixed, never buried. (2) FIX -- normally the count that will be auto-applied and land in the reviewable remediation CL; in REVIEW MODE nothing is auto-applied, so render it as the count PROPOSED and awaiting the step-4 decision, never as applied. (3) IMPROVE -- 'Audit found <N> improvement opportunit(ies). Do you want to discuss them?' plus one one-line pitch each. SILENT findings do NOT appear. If a section's count is zero, omit that section."
          expected: "Markdown report: per-file verdicts, then SERIOUS (summarized, top) / FIX (applied count) / IMPROVE (count + one-liners); SILENT omitted."
        - n: 4
          action: "Q&A GATE. If review is TRUE: NOTHING is auto-applied -- FIX is demoted from auto-apply to PROPOSED and goes to the user alongside IMPROVE/SPECIAL. Present proposals with AskUserQuestion offering accept-all / reject-all / custom instruction (use multiSelect when accept-some is the natural shape). Use judgement on grouping: batch fixes across files into ONE question when they are small and the files are related; split into several when the fixes are large or the files are unrelated. SERIOUS is surfaced at the top as always and still never auto-fixed. Review-mode declines write NOTHING to `md-audit-declined:` -- that ledger is IMPROVE-scoped and per-file-permanent, whereas a review decline usually means 'not in this change', and once the change lands the finding is in the next pre-image and stops being attributable anyway. Offer an explicit 'never flag this again for this file' only if the user asks for it, and only then write the ledger. If review is FALSE and non_interactive is FALSE (default): SERIOUS findings are surfaced summarized at the top and never auto-fixed (the user decides the real-world fix); for each IMPROVE and SPECIAL finding the user opted to discuss, ask for a decision (apply as-proposed / skip / a refined instruction). Surface a tight grouped set; do not dump a giant list. A declined IMPROVE is recorded in the file's `md-audit-declined:` frontmatter so a re-audit does not re-pitch it. If non_interactive is TRUE: apply FIX findings, surface SERIOUS, and infer each IMPROVE/SPECIAL decision from the taxonomy's default_remediation plus the file content -- record each inferred decision in the final summary so the user can see and reverse them. FIX findings need no decision (they apply by definition); SILENT findings are never surfaced."
          expected: "SERIOUS summarized; a decision (explicit or inferred) attached to every IMPROVE/SPECIAL the user engaged; FIX applied."
        - n: 5
          action: "REMEDIATE phase (after-Q&A). Assemble per-file remediation lists from the decided findings (FIX=apply; IMPROVE/SPECIAL=per decision; SERIOUS never auto-applied; drop skips). Choose mode by how many FILES carry remediation work. ONE file: apply inline with Edit. TWO OR MORE files: call the Workflow tool with scriptPath ${CLAUDE_PLUGIN_ROOT}/skills/claude-md-audit/workflow/remediate.js and args = { perFile:[{path,role,remediations:[{criterion,taxonomy,bucket,line,instruction,decision}]}] }. One lane per file (disjoint files never conflict)."
          tool: "Workflow | inline"
          expected: "Edits applied to the target files; per-file applied/skipped/failed summary."
        - n: 6
          action: "Render the final summary: FIX applied per file, IMPROVE decisions, SERIOUS still-open (never auto-applied), any failures. Remind the user that re-running the audit should now reproduce a clean (or reduced-FAIL) verdict -- detection and remediation are separate passes, so the re-run is the verification step. Scope the verification re-run to the files that were actually MODIFIED by remediation -- results for untouched files stand; re-auditing them wastes runs."
          expected: "Closing summary; user can re-run /md-audit claude-md on the modified files to verify FAILs cleared."
      output_template: |
        ## <file path> (<role>)

        Lines: <N> / Tokens: <N> / Findings: <count by bucket>

        ### CCP (write-together / change cadence)
        [PASS|FAIL|JUDGMENT] <criterion>: <message>

        ### CRP (read-together / smallest correct scope)
        [PASS|FAIL|JUDGMENT] <criterion>: <message>

        ### ADP (link-forward-only / DAG)
        [PASS|FAIL|JUDGMENT] <criterion>: <message>

        ### Hygiene (universal)
        [PASS|FAIL|INFO] <criterion>: <message>

        ### Schema (when claude_md: YAML block present)
        [PASS|FAIL] <yaml row>: <message>

        ### CodeDir (when dimension = code-directory)
        [PASS|FAIL|JUDGMENT|INFO] <CD-criterion> <taxonomy>: <message>

        ### Density (when the density lens was requested)
        [JUDGMENT] <DD-criterion> <taxonomy>: <message> (routes: tighten | extract->ref | merge; ~<N> tokens)

        ### Compliance verdict

        <P> PASS / <F> FAIL / <I> INFO / <J> JUDGMENT-REQUIRED
        Verdict: COMPLIANT | NON-COMPLIANT

        ## Report (SERIOUS -> FIX -> IMPROVE; SILENT omitted, no hedging)

        ### SERIOUS -- Found <N> serious issue(s) that require fixing
        - <one-line summary per issue>   (never auto-fixed; the fix is a real-world action)

        ### FIX -- <N> applied (in the reviewable remediation CL)   [review mode: "<N> proposed" -- nothing is applied]
        - <criterion>: <what was corrected>

        ### IMPROVE -- Audit found <N> improvement opportunit(ies). Do you want to discuss them?
        - <criterion>: <one-line pitch>
      gotchas:
        - "Role classification is anchored on cwd (the directory claude was launched in). The cwd CLAUDE.md is `root` only when no CLAUDE.md exists above it; if an ancestor CLAUDE.md is found, the cwd file is classified `child` so the project-root-only hygiene checks (H1/H2/H3) do not fire on a subordinate file and the parent-child duplication check runs against the ancestor."
        - "INFO findings are advisory (size signals, migration opportunities). They do NOT escalate to FAIL on subsequent runs even if unaddressed."
        - "When auditing a child CLAUDE.md, the parent must be read for CCP duplication checks. If the parent cannot be located (e.g. standalone file with no project context), report 'parent unavailable' for parent-relative criteria rather than failing them silently."
        - "For role=local (CLAUDE.local.md), only D-group criteria apply (see role-to-criteria map). Hygiene and ADP rules are skipped because the file is by design personal-scoped."
        - "The density lens (DD-1..DD-4, group Density) is OPT-IN and ADVISORY: it loads references/density-criteria.md only when density_lens is true, every finding is JUDGMENT with disposition IMPROVE, and it never produces FAIL. A density-only run is always COMPLIANT -- the lens surfaces token-efficiency improvement opportunities, it never gates. Density findings do not change the verdict and do not escalate on re-runs."
        - "Density vs the classic criteria -- do not double-count: O_low_value_verbose (DD-4) is the CLASSIC-file value filter and must not fire on a code-directory file (CD-5/J owns value there); M_extract_to_reference (DD-2) is a disclosure-level move within one scope, distinct from A_wrong_role_content (different file) and finer than C_crp_split_candidate (whole-file split); N_intra_file_redundancy (DD-3) is within one file, distinct from B (across the role chain, FAIL/FIX)."
        - "Disposition is instance-level, not a fixed property of the taxonomy id: the `bucket` field is only the DEFAULT starting point; the detect.js step-8 classifier assigns FIX / SERIOUS / IMPROVE / SILENT per finding against the explicit predicates. Same file, same finding -> same disposition (idempotent)."
  # Disposition mapping (four-disposition model): the structural lanes below are
  # retained for schema stability across all audit members. auto = FIX categories
  # (auto-applied; land in the reviewable CL). discuss = SERIOUS (surfaced at top,
  # summarized, NEVER auto) + IMPROVE (count + one-liners, opt-in) categories,
  # disposition noted per entry. special = K escape hatch. The FINAL per-finding
  # disposition is assigned instance-level by the detect.js step-8 classifier;
  # these defaults are the starting point.
  remediations:
    auto:
      - category: "B_ccp_cross_file_duplication"
        procedure: "[FIX] Delete the restated rule from the child file (the parent rule loads automatically when the agent descends into the child directory). Loss-free-deletion guard first: fold any child-local delta into the parent SSOT or a REMINDER-PLUS-REFERENCE summary line before deleting."
        agent_template: "Background agent receives child CLAUDE.md path + duplicated-rule line range + parent rule reference. Folds any local delta into the parent, applies the deletion, confirms the parent rule is still present."
      - category: "I2_line_drift"
        procedure: "[FIX] Remove the stale line number from the claim, keeping the symbol anchor. The symbol resolves; only the number rotted."
        agent_template: "Background agent receives the claim line + the cited (drifted) line number + the resolved symbol location. Strips the number, leaves the symbol reference intact."
      - category: "E_schema_failure"
        procedure: "[FIX default] Add a missing field with a sensible default (decidable by convention). An authorial-judgment field routes to IMPROVE (offer as a one-liner)."
        agent_template: "Background agent receives the failing schema rows; adds each missing-default field; reports authorial rows back for the IMPROVE lane."
      - category: "P_stale_factual_claim"
        procedure: "[FIX default] Recount and correct the number against unambiguous ground truth; prefer the information-preserving fix (correct the count AND add the missing entry). An ambiguous / possibly-intentional discrepancy routes to IMPROVE (offer as a one-liner)."
        agent_template: "Background agent receives the stale claim + current ground truth; applies the mechanical correction; reports ambiguous cases back for the IMPROVE lane."
      - category: "Q_skill_content_duplication"
        procedure: "[FIX] Trim to a one-line guardrail naming the rule/failure mode plus a pointer to the owning skill (per C-5/A-4) -- dedup under the summarize-and-reference rule (REMINDER PLUS REFERENCE, a dozen tokens or less, else reference-only). Loss-free-deletion guard first: fold any local delta into the guardrail line."
        agent_template: "Background agent receives the restated block + owning skill/reference; folds any local delta into a guardrail line, replaces the block with reminder-plus-pointer."
      - category: "H_stale_anchor"
        procedure: "[FIX default] Re-anchor the claim to the found current symbol/path, or delete it if the code is gone (falsified content). SERIOUS instead when the anchor guards a protective rail with NO surviving mechanism -- surface the unprotected invariant at the top, do not auto-fix."
        agent_template: "Background agent receives the unresolved anchor + the found target (or absence proof); re-anchors or deletes; escalates a no-surviving-mechanism rail to the SERIOUS surface."
      - category: "I_claim_drift"
        procedure: "[FIX default] Update the mechanism/magnitude or retire the claim when the actual behavior was verified from the code reading (the code reading is evidence). IMPROVE only when no fact decides. Counted magnitudes alone are never flagged."
        agent_template: "Background agent receives the claim + the contradicting code read; applies the verified correction."
      - category: "J_low_value_insight"
        procedure: "[FIX default] Delete a bare un-annotated inventory / default / restatement (be aggressive), after the loss-free-deletion guard. A trim of TRUE content passing the one-line test -> IMPROVE; a validator artifact / historical record / accepted pattern -> SILENT (not surfaced)."
        agent_template: "Background agent receives the low-value section + the value-filter reasoning; deletes bare inventory after folding any local delta; routes true-content trims to IMPROVE."
      - category: "R_ancestor_convention_violation"
        procedure: "[FIX default] Apply the mechanical correction that satisfies the verbatim-quoted ancestor convention (replace the non-ASCII look-alike, relativize the absolute path, apply the stated formatting rule). SERIOUS instead when the violation reveals a real-world problem the rule exists to prevent (e.g. a committed secret) -- surface at the top, never auto-fix."
        agent_template: "Background agent receives the subject line + the verbatim ancestor rule quote + the ancestor source path; applies the convention-satisfying edit, or escalates a real-world-problem violation to the SERIOUS surface."
    discuss:
      - category: "H2_inverted_absence"
        procedure: "[SERIOUS] Surface at the TOP of the report, summarized -- the invariant the claim guards is violated (the asserted-absent thing is now present); the doc problem reveals a real-world problem. The remediation is in the repo, not the CLAUDE.md. NEVER auto-fixed."
      - category: "A_wrong_role_content"
        procedure: "[IMPROVE] Propose moving the misplaced section to the correct-scope CLAUDE.md. Show the destination and the line range to move. One-line pitch; user opts in."
      - category: "C_crp_split_candidate"
        procedure: "[IMPROVE] Propose an L1 -> L2/L3 decomposition: which sections move to a SKILL.md, which become reference docs, and the triggering criteria per reference. One-line pitch; user opts in before splitting."
      - category: "D_adp_forward_dependency"
        procedure: "[IMPROVE] Surface the forward reference. Ask user: inline the descendant content (rule is parent-scoped) or remove the reference (rule is descendant-scoped)? One-line pitch; apply the user's choice."
      - category: "F_hygiene_threshold"
        procedure: "[IMPROVE] Run the CRP test (do body sections serve different reading tasks?). If yes, escalate to C. If no, INFO stays; the larger CLAUDE.md is correct."
      - category: "G_descendant_role_mismatch"
        procedure: "[IMPROVE] Propose moving project-conventional content from .local file into the checked-in CLAUDE.md (so all collaborators see it). One-line pitch; user opts in before applying."
      - category: "L_verbose_in_place"
        procedure: "Show the over-worded section and a tightened rewrite (or the sentences to compress) with an approximate token-savings figure. Tighten IN PLACE only -- never move or delete. User confirms; honor carve-outs (teaching examples, load-bearing nuance, labeled safety-rail repetition)."
      - category: "M_extract_to_reference"
        procedure: "Propose moving the self-contained on-demand block to a named reference doc (or a SKILL.md for on-task procedure), leaving a one-line pointer behind, with an approximate token-savings figure. Disclosure-level move within the same scope; user confirms before extracting."
      - category: "N_intra_file_redundancy"
        procedure: "Show the repeated statements; propose keeping the single best one and cross-referencing the others. User confirms which survives."
      - category: "O_low_value_verbose"
        procedure: "Show the section and why it fails the value lattice (code-dir-insight-filter.md Step 4) after carve-outs. Propose downgrade-to-a-line or, for a contentless section, deletion. User confirms; this lens never auto-deletes."
      - category: "N_user_standard_violation"
        procedure: "[severity-driven; never auto-applied] fail -> SERIOUS (surface at the top, never auto-fixed -- the auditor cannot mechanically satisfy an arbitrary user-authored standard, so the user addresses it); info -> IMPROVE (one-line pitch); judgment -> JUDGMENT (surface for review). The message quotes the verbatim criterion statement + criterion id + source standards-file path. enforcement: mechanical criteria are NOT handled here -- audit.py enforces them under --config."
    special:
      procedure: "Surface the finding with the audit row that fired, attempted categories, and reasons none fit. User proposes strategy. Generalizable strategies become new taxonomy categories in references/audit-criteria.md."
  enforcement:
    gate_kind: "audit-finding"
    gating_rule: "FAIL findings (CCP cross-file duplication, ADP forward dependency, schema validation failures with non-optional missing fields, N-user-standard violation of a fail-severity criterion) gate compliance. JUDGMENT findings surface for review without gating; INFO findings are advisory only."
    appeal_process: "JUDGMENT findings are resolved by user confirmation (PASS once the user accepts the exception explicitly). FAIL findings have no bypass; remediation is available within the taxonomy."
  gotchas:
    - "The subject is a corpus of CLAUDE.md files, but the audit procedure visits one file at a time. The role-to-criteria map ensures the right criteria apply to the right file."
    - "Role classification is cwd-relative. A standalone audit of a single file outside a project tree will classify it as root by default; surface that assumption if it affects criteria."
    - "Schema validation is conditional -- only files carrying a `claude_md:` YAML contract block are checked. Files without the block are never FAILed for its absence. Root-role files SHOULD carry the block (the authoring path adds it); a pre-existing root file without one gets an INFO, not a FAIL."
    - "Idempotency: criteria, taxonomy, and bucket assignments are fixed. Same input produces the same verdict; do not re-rank or re-order findings session-to-session."
  anti_patterns:
    - id: "audit_then_self_remediate"
      name: "Audit and remediate in the same procedure pass"
      keywords: ["self-remediation", "single-pass", "idempotency"]
      why_it_seems_right: "Auditing one file and applying remediations in the same pass seems efficient -- one tool call, fewer round trips."
      why_it_is_wrong: "Mixing detection and remediation in one pass breaks idempotency. The verdict and remediation are separate phases; conflating them prevents re-runs from producing the same findings."
      alternative: "Run the audit procedure to completion. Render the verdict. Dispatch remediations as separate FIX (auto-applied; proposed instead in review mode) + IMPROVE (opt-in) work units, surface SERIOUS at the top. Re-run the audit after remediation to verify."
    - id: "duplicate_parent_rule_for_convenience"
      name: "Restate a parent rule in a child file 'for convenience'"
      keywords: ["duplication", "parent rule", "child file", "ccp violation"]
      why_it_seems_right: "Stating the rule in both places means a reader of the child file does not have to consult the parent -- seems more usable."
      why_it_is_wrong: "Duplication violates CCP and creates two sources of truth that drift. The agent always loads the parent CLAUDE.md when descending into the child; the rule is already in context."
      alternative: "Trust the load model. State the rule once at the correct role. If the child file is meant to be read standalone (e.g. distributed without the parent), note that explicitly and consider whether the parent rule belongs at the child's role instead."
```

## Argument grammar

- `(none)` -- audit `<cwd>/CLAUDE.md`.
- `list` -- show numbered list of CLAUDE.md files visible from cwd; do not audit.
- `<path>` -- audit a specific CLAUDE.md or CLAUDE.local.md.
- `<numbers>` -- audit files by index from the most recent `list` output (e.g. `3 7 9`).
- `fast` / `--fast` / `--yes` / `-y` -- non-interactive: skip the Q&A round and infer every IMPROVE/SPECIAL decision (see Non-interactive mode); FIX applies by definition, SERIOUS is surfaced. Combine with any selector, e.g. `/md-audit claude-md 3 7 fast`. Prose intent ("audit these and just apply everything, don't ask me") sets the same flag.
- `review` / `--review` -- review mode: audit a CHANGE rather than a file. Findings the change did not cause are suppressed, nothing is auto-applied (FIX is proposed, not applied), and the verdict is `DIFF-CLEAN` rather than `COMPLIANT`. For gating a submit / publish / handback. Combine with any selector, e.g. `/md-audit claude-md CLAUDE.md --review`. Prose intent ("review my changes before I submit", "audit the diff") sets the same flag. Mutually exclusive with `fast` -- see Review mode. Off by default.
- `density` / `--density` -- add the opt-in density lens (DD-1..DD-4: verbosity-in-place, extract-to-reference, intra-file redundancy, value-earns-tokens). Advisory only -- all findings are JUDGMENT, disposition IMPROVE, never FAIL, so a density-only run is always COMPLIANT. Combine with any selector, e.g. `/md-audit claude-md 3 density`. Prose intent ("is this CLAUDE.md too verbose", "can anything move to a reference", "audit for token efficiency") sets the same flag. Off by default; the lens never runs unless requested.

Typical workflow: `/md-audit claude-md list` to see what's available, then `/md-audit claude-md 3 7` to audit specific files. Add `density` to also surface token-efficiency opportunities: `/md-audit claude-md 3 density`.

## Workflow orchestration

This skill runs in two phases split by an interactive Q&A gate, and uses the Workflow tool to fan the work out across files. **Invoking this skill authorizes the Workflow-tool calls described below** (the skill's instructions are the opt-in; do not re-prompt the user for permission to orchestrate).

```
resolve (main loop)
  -> DETECT  (before-Q&A)  : 1 file inline | 2+ files via workflow/detect.js   -> structured findings
                             (review mode: ALWAYS via workflow/detect.js, any file count)
  -> render report (main loop)
  -> Q&A GATE (main loop)  : interactive decisions | inferred when non-interactive
  -> REMEDIATE (after-Q&A) : 1 file inline | 2+ files via workflow/remediate.js -> edits applied
  -> final summary + "re-run to verify"
```

**Multi-file threshold (the overhead equalizer).** The Workflow tool has real per-run overhead (background orchestration, agent spin-up). For a single file that overhead is not worth it, so a 1-file audit runs inline in the main loop. At 2+ files the parallel fan-out pays for itself, so detection (and, separately, remediation) go through the workflow scripts. **Fallback when the Workflow tool is not exposed** (subagent environments do not have it): run the 1-file inline procedure sequentially per file -- detection for all files first, then remediation, keeping the two as separate passes with the Q&A gate between them. **This fallback does NOT apply in review mode.** Inline detection inherits the session's model, which is exactly the property review mode's threshold-1 override exists to eliminate -- a gate whose strictness depends on which model the caller happened to be running is not a gate. If review mode is requested where the Workflow tool is unavailable, either stop and tell the user review mode needs a main-session run, or run inline and label the result explicitly as advisory-and-unpinned, never as a passed gate. Detection and remediation are **always separate passes** even in workflow mode -- the interactive Q&A sits between them, and a background workflow cannot ask the user anything. This split is also what preserves the `audit_then_self_remediate` anti-pattern: re-running the audit reproduces the same findings because nothing was remediated during detection.

**The two workflow scripts** (hand-authored, shipped as skill assets):

- `workflow/detect.js` -- before-Q&A. One lane per file: read (+parent if child) -> apply criteria -> schema-validate -> classify. Returns `{ perFile, totals }`. No edits.
- `workflow/remediate.js` -- after-Q&A. One lane per file (disjoint files, no conflicts): apply the decided edits. Returns `{ perFile, summary }`.

Both accept `args` as an object or JSON string. Pass absolute `refs` paths (they run from the session cwd, not the skill dir).

## Non-interactive mode

When the non-interactive flag is set (argument token or expressed intent), the Q&A gate does not prompt. Instead, infer each IMPROVE/SPECIAL decision from the taxonomy's `default_remediation` plus the file content, apply them, and **list every inferred decision in the final summary** so the user can see and reverse them. FIX findings apply regardless; SERIOUS findings are surfaced summarized at the top and never auto-applied; SILENT findings are never surfaced. FAIL findings are still gated by the verdict; non-interactive only changes how the *decisions* are obtained, not the audit contract. Interactive mode is the default; non-interactive is opt-in per the rule above.

## Review mode

Normal mode audits a FILE. Review mode audits a CHANGE: same criteria, same lanes, but findings the change did not cause are suppressed and nothing is auto-applied. It exists to gate a submit / publish / handback, where a report full of pre-existing findings would either bloat the change with unrelated remediations or train the author to skim past the gate.

Three behavioral differences, and nothing else:

1. **Attributability filter.** Each finding is marked `attributable` by the lane, then the caller drops the ones the change did not cause. **SERIOUS always survives regardless** -- a secret or a violated invariant is not the author's doing and is still the most important thing on the page.
2. **Nothing is auto-applied.** FIX is demoted to a proposal at the Q&A gate. Mutually exclusive with `fast`, which would mean "propose instead of applying, but do not ask" -- i.e. nothing. Reject that combination rather than guessing.
3. **Verdict is `DIFF-CLEAN`, not `COMPLIANT`.** A weaker and more honest claim: *this change introduced no failure*, not *this file is clean*. A DIFF-CLEAN file may still carry a surviving SERIOUS.

Also: the multi-file threshold drops to 1 (always use the Workflow path), because a submit gate must not inherit whatever model the session happens to be running.

**You materialize the pre-images; the workflow never does.** This plugin is VCS-agnostic and must stay that way -- do not teach `detect.js` about Perforce or git. Before calling the workflow, write each file's pre-change content to a temp path and pass it as `preImagePath`:

- Perforce: `p4 print -q -o <tmp> //depot/path/FILE#have`
- git: `git show <base>:<path> > <tmp>`, base = `merge-base(HEAD, origin/main)`, with the diff spanning `base..worktree` so committed-but-unpushed work is INSIDE the change under review rather than part of its baseline.

Infer which from the local repo. Two cases that are easy to get wrong:

- **Adds have no pre-image.** Pass `preImagePath: null` and every finding is attributable, which is correct -- the whole file is new. `p4 diff` emits nothing for an add, so detect adds via `p4 opened` rather than concluding the diff is unavailable.
- **Moves need the source.** `p4 print //new/path#have` fails for a `move/add`; resolve the pre-image through the move source.

For a CHILD file, also pass `parentPreImagePath` so cross-file duplication the change introduced *in the parent* is not misattributed to the untouched child.

**If you cannot obtain a pre-image, do not silently fall back to a whole-file audit.** Say the pre-image was unavailable and label the output as unfiltered, so nobody mistakes a normal audit for a change-scoped gate.

Two limits worth stating rather than hiding:

- **Cross-file findings can escape.** A change touching only a parent can create duplication whose finding anchors on an untouched child -- which is not in the change's file set and never gets a lane.
- **Attributability is judgment, not arithmetic.** It rests on re-detection, so a pre-existing finding the pre-image check happens to miss can resurface as attributable. Generous structural matching mitigates this; nothing eliminates it.

## Decision rules

- Any FAIL finding -> file is NON-COMPLIANT.
- Only PASS / INFO / JUDGMENT findings -> file is COMPLIANT.
- INFO findings are advisory improvements, not compliance failures, and do not escalate to FAIL on subsequent runs.
- **Review mode:** any *attributable* FAIL -> NON-COMPLIANT; otherwise DIFF-CLEAN. Non-attributable FAILs do not gate -- they predate the change -- but a non-attributable SERIOUS is still reported above the verdict.

## Cross-references

- Canonical placement framework: `cohesion-principles (in skills-kit)`. The criteria in this skill's `references/audit-criteria.md` derive directly from that skill's content_allocation framework; when the two diverge, the canonical framework wins.
- Schema validation tooling: `plugins/skills-kit/skills_kit_lib/audit.py` (run as `python -m skills_kit_lib.audit` from the plugin root; validates `claude_md:` YAML blocks against `CLAUDE_MD_SCHEMA` in `skills_kit_lib/schemas/claude_md.py`).
- Code-directory insight-validation criteria: `references/code-dir-insight-filter.md` (the self-contained CD-1..CD-6 dimension, loaded only for dimension=code-directory files). The Level-1 trigger that flags the dimension lives in `scripts/discover.py::classify_dimension`.
- Density lens criteria: `references/density-criteria.md` (the self-contained DD-1..DD-4 opt-in lens for verbosity/disclosure, loaded only when the `density` arg or equivalent intent is given). It reuses the value lattice in `references/code-dir-insight-filter.md` Step 4 rather than restating it. Advisory only -- never gates compliance.
- Authoring counterpart: `claude-md-authoring:references/code-directory-claude-md.md` -- authoring code-directory CLAUDE.md files to that doc is what keeps this audit green (same four shapes, observation taxonomy, anchoring + path discipline).
- Sibling audit skills: `skill-audit` (via `/md-audit skill`) for SKILL.md files; `project-doc-audit` (via `/md-audit project-doc`) for standalone project documents (Docs/, .claude/docs/, READMEs); `references-audit` (via `/md-audit references`) for broken cross-references across markdown.
