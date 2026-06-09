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
      - "the opt-in density lens (DD-1..DD-4) -- verbosity-in-place, extract-to-reference, intra-file redundancy, value-earns-tokens; advisory only (JUDGMENT/DISCUSS, never FAIL/AUTO); self-contained in references/density-criteria.md; runs only when the `density` argument or equivalent intent is given"
      - "categorizing findings into remediation buckets (AUTO / DISCUSS / SPECIAL)"
      - "listing CLAUDE.md files visible from cwd (the cwd-relative discover.py helper for index-based selection)"
    excludes:
      - "auditing SKILL.md files (use /skill-audit)"
      - "auditing reference docs (audited transitively via the SKILL.md they belong to)"
      - "auditing cross-references between skills or docs (use /references-audit)"
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
      detail: "Mechanical validation via audit.py when the block is present. Conditional: applies only when the file declares the contract."
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
      detail: "Coupled to symbol resolution; never fires when no line number is cited. Bucket AUTO (the remediation is the mechanical removal of the stale number)."
    - id: "cd_fidelity_claim_holds"
      name: "CodeDir -- claim still matches the code in kind"
      keywords: ["code-directory", "claim drift", "stale claim", "in kind", "counted magnitude"]
      summary: "Read the anchored code; if the claim no longer holds in kind (god-object now decomposed, TODO now resolved, bypass now gone) flag I_claim_drift. Counted magnitudes ('7200-line', '12 files') are intentionally fuzzy -- never FAIL on the number; flag only on kind-inversion."
      severity: "JUDGMENT"
      detail: "Never auto-FAIL: the audit cannot perfectly re-derive the gotcha, it flags divergence for a human. Bucket DISCUSS."
    - id: "cd_value_insight_earns_place"
      name: "CodeDir -- section earns its place under the what-we-care-about filter"
      keywords: ["code-directory", "value filter", "earns place", "low value", "inventory", "carve-out"]
      summary: "Each section must pass the value lattice (silent-failure > blast-radius > deliberately-wrong > safety > perf > ownership). Linter-caught / default / bare-inventory / pure-restatement sections are low-value (J). Honor every carve-out: annotated Files/Schema blocks, SSOT-pointing catalogs, safety-rail cheatsheets, topology tables are NOT low-value."
      severity: "JUDGMENT"
      detail: "Bucket DISCUSS; deletion of a genuinely bare un-annotated inventory may be AUTO. Carve-outs are load-bearing -- both maintainer-agents required them."
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
      detail: "Opt-in density lens, loaded only on the `density` request. Advisory: never FAIL, never AUTO. Self-contained in references/density-criteria.md (DD-1). Bucket DISCUSS; remediation must route tokens (tighten in place)."
    - id: "dd_extract_to_reference"
      name: "Density -- self-contained block should be disclosed to a reference"
      keywords: ["density", "disclosure", "extract to reference", "progressive disclosure", "L1 to L3", "on-demand block"]
      summary: "A self-contained block serving an on-demand/narrow reading task, large enough that inlining taxes every reader, should move to a references/*.md (or SKILL.md for on-task procedure) leaving a one-line pointer. Disclosure-level move, not scope-level -- distinct from crp_role_appropriate (wrong file) and finer than C_crp_split_candidate (whole-file split)."
      severity: "JUDGMENT"
      detail: "Opt-in density lens (DD-2). Advisory: never FAIL, never AUTO. Bucket DISCUSS; remediation names the destination reference and the pointer left behind."
    - id: "dd_intra_file_redundancy"
      name: "Density -- same fact stated more than once within one file"
      keywords: ["density", "intra-file duplication", "redundancy", "state once", "cross-reference"]
      summary: "The same fact stated multiple times within THIS file (distinct from ccp_cross_file_duplication, which is across the role chain and FAIL/AUTO). Keep the single best statement; replace the others with a cross-reference."
      severity: "JUDGMENT"
      detail: "Opt-in density lens (DD-3). Advisory: never FAIL, never AUTO. Bucket DISCUSS; remediation names which statement survives."
    - id: "dd_value_earns_tokens"
      name: "Density -- section does not earn its tokens (classic-file value filter)"
      keywords: ["density", "value filter", "earns tokens", "low value verbose", "downgrade"]
      summary: "The classic-file generalization of the code-directory value filter: a section that does not earn its tokens under the value lattice AND is verbose about it. Defers the ranking + carve-outs to code-dir-insight-filter.md Step 4 (SSOT). Does NOT double-count with CD-5/J -- on a code-directory file, value findings stay in CD-5."
      severity: "JUDGMENT"
      detail: "Opt-in density lens (DD-4). Advisory: never FAIL, never AUTO. Bucket DISCUSS; remediation proposes downgrade-to-a-line or confirmed deletion of a contentless section."
  taxonomy:
    - id: "A_wrong_role_content"
      name: "Content sits at the wrong role in the CLAUDE.md hierarchy"
      keywords: ["wrong role", "wrong scope", "child rule in root", "root rule in child"]
      detection_signal: "Agent judgment from cohesion-principles role-to-criteria map. Body section's scope is narrower or broader than the file's role allows."
      default_remediation: "Propose moving the section to the correct-scope CLAUDE.md (e.g. narrow root rule -> subdirectory CLAUDE.md; broad subdir rule -> project root CLAUDE.md). User confirms the move."
      bucket: "DISCUSS"
    - id: "B_ccp_cross_file_duplication"
      name: "Rule restated from parent CLAUDE.md"
      keywords: ["duplication", "parent rule", "inheritance violation", "redundant"]
      detection_signal: "Body restates a rule already present in an ancestor CLAUDE.md (read during the audit's role-walk phase)."
      default_remediation: "Delete the restated rule from the child file. The parent rule is loaded automatically when the agent descends into the child."
      bucket: "AUTO"
    - id: "C_crp_split_candidate"
      name: "Body sections serve different reading tasks (CRP split warranted)"
      keywords: ["crp split", "different reading tasks", "progressive disclosure", "decomposition"]
      detection_signal: "Body over size threshold AND agent judgment that sections genuinely serve different reading tasks (e.g. setup-time rules + on-task triggers + reference glossary)."
      default_remediation: "Propose an L1 -> L2 / L3 decomposition: move on-task content to a SKILL.md (L2); move reference content to a reference doc (L3). User confirms before splitting."
      bucket: "DISCUSS"
    - id: "D_adp_forward_dependency"
      name: "Parent CLAUDE.md depends on descendant content"
      keywords: ["adp", "forward dependency", "graph cycle", "wrong load order"]
      detection_signal: "Body references or assumes content from a descendant CLAUDE.md (e.g. 'see subsystem/CLAUDE.md for the rule')."
      default_remediation: "Either inline the descendant content into the parent (if the rule is truly parent-scoped) or remove the forward reference (if the rule is descendant-scoped and the parent has no business assuming it). User confirms."
      bucket: "DISCUSS"
    - id: "E_schema_failure"
      name: "claude_md: YAML block fails schema validation"
      keywords: ["schema fail", "claude_md schema", "yaml validation", "contract block"]
      detection_signal: "audit.py reports schema validation failure for the file's claude_md: YAML block (missing required key, wrong type, forbidden key)."
      default_remediation: "Surface the failing rows. Missing fields with sensible defaults are AUTO sub-cases; authorial fields are DISCUSS."
      bucket: "DISCUSS"
    - id: "F_hygiene_threshold"
      name: "Body over size threshold (CRP-evaluation prompt)"
      keywords: ["hygiene", "size threshold", "line count", "token count"]
      detection_signal: "Mechanical INFO finding: body line count > 500 or token count > 3000."
      default_remediation: "Run the CRP test (do sections serve different reading tasks?). If yes, escalate to C. If no, INFO stays as-is."
      bucket: "DISCUSS"
    - id: "G_descendant_role_mismatch"
      name: "Local file (.local) carries non-local content"
      keywords: [".local", "personal scope", "machine-specific", "wrong file"]
      detection_signal: "CLAUDE.local.md body contains project-conventional content that should be in the checked-in CLAUDE.md instead of a personal override."
      default_remediation: "Propose moving the project-conventional content to the checked-in CLAUDE.md (so all collaborators see it). User confirms before moving."
      bucket: "DISCUSS"
    - id: "H_stale_anchor"
      name: "CodeDir: requires-present anchor no longer resolves"
      keywords: ["code-directory", "stale anchor", "broken symbol", "missing sibling", "fidelity"]
      detection_signal: "A `requires-present` anchor (symbol / file / sibling / field the claim says should exist) is absent after a repo-wide check, and is not classified external / generated / template / vendored."
      default_remediation: "Re-anchor the claim to the current symbol/path, or delete the claim if the code it describes is gone. User confirms."
      bucket: "DISCUSS"
    - id: "H2_inverted_absence"
      name: "CodeDir: requires-absent thing is now present"
      keywords: ["code-directory", "negative existence", "tracked secret", "forbidden present", "invariant violated"]
      detection_signal: "A `requires-absent` claim's asserted-absent thing now exists (a tracked file under a gitignored SSOT path; a FORBIDDEN name that now resolves)."
      default_remediation: "Surface loudly -- the invariant the claim guards is violated. The fix is in the code/repo, not the CLAUDE.md. User decides."
      bucket: "DISCUSS"
    - id: "I_claim_drift"
      name: "CodeDir: claim no longer matches the code in kind"
      keywords: ["code-directory", "claim drift", "stale claim", "in kind"]
      detection_signal: "Reading the anchored code contradicts the claim in kind (decomposed god-object, resolved TODO, bypass now gone). NOT a counted-magnitude difference."
      default_remediation: "Re-validate with the user; update the mechanism/magnitude or retire the claim."
      bucket: "DISCUSS"
    - id: "I2_line_drift"
      name: "CodeDir: cited line number drifted from its symbol"
      keywords: ["code-directory", "line drift", "re-anchor", "drop line number"]
      detection_signal: "The enclosing symbol the claim names resolves but is >~30 lines from the cited number, and the author gave no recovery hint."
      default_remediation: "Drop the line number; keep the symbol anchor."
      bucket: "AUTO"
    - id: "J_low_value_insight"
      name: "CodeDir: section fails the what-we-care-about value filter"
      keywords: ["code-directory", "low value", "bare inventory", "restatement", "value filter"]
      detection_signal: "A section is linter-caught / a language default / a bare un-annotated inventory / a pure schema restatement -- AND not protected by a carve-out (annotated Files/Schema, SSOT-pointing catalog, safety-rail cheatsheet, topology table)."
      default_remediation: "Propose deletion (bare inventory) or downgrade. User confirms; bare-inventory deletion may be AUTO."
      bucket: "DISCUSS"
    - id: "L_verbose_in_place"
      name: "Density: correctly-placed section is over-worded"
      keywords: ["density", "verbose", "tighten in place", "ceremony", "token reduction"]
      detection_signal: "Density lens (DD-1): a valuable, correctly-scoped section uses materially more words than its information content requires, and is not protected by a carve-out (teaching example, load-bearing nuance, labeled safety-rail repetition)."
      default_remediation: "Propose a tightened rewrite (or the specific sentences to compress) IN PLACE, with an approximate token-savings figure. Never move or delete. User confirms."
      bucket: "DISCUSS"
    - id: "M_extract_to_reference"
      name: "Density: self-contained block should move to a reference"
      keywords: ["density", "disclosure", "extract to reference", "pointer", "progressive disclosure"]
      detection_signal: "Density lens (DD-2): a self-contained on-demand block is large enough to tax every reader who does not need it; it belongs one disclosure level deeper (references/*.md or a SKILL.md) within the same scope."
      default_remediation: "Propose moving the block to a named reference doc and leaving a one-line pointer behind, with an approximate token-savings figure. User confirms before extracting."
      bucket: "DISCUSS"
    - id: "N_intra_file_redundancy"
      name: "Density: same fact stated more than once within one file"
      keywords: ["density", "intra-file duplication", "redundancy", "state once"]
      detection_signal: "Density lens (DD-3): a fact is restated in multiple sections of THIS file (not across the role chain -- that is B)."
      default_remediation: "Propose keeping the single best statement and replacing the others with a cross-reference. User confirms which survives."
      bucket: "DISCUSS"
    - id: "O_low_value_verbose"
      name: "Density: section does not earn its tokens (classic-file value filter)"
      keywords: ["density", "low value", "value filter", "downgrade", "earns tokens"]
      detection_signal: "Density lens (DD-4): a classic-file section fails the value lattice (code-dir-insight-filter.md Step 4) AND is verbose. Not run on code-directory files (CD-5/J owns value there)."
      default_remediation: "Propose downgrade (compress to a line) or, for a contentless section, deletion. User confirms; this lens never auto-deletes."
      bucket: "DISCUSS"
    - id: "K_unclassified"
      name: "Unclassified / special case"
      keywords: ["unclassified", "special case", "escape hatch", "K bucket"]
      detection_signal: "Finding does not match any A-G or H-J detection signal after deliberate attempt."
      default_remediation: "Surface to the user with the audit row that fired, attempted matches, and reasons none fit. User proposes strategy."
      bucket: "SPECIAL"
  procedures:
    - id: "audit_claude_md"
      name: "Audit one CLAUDE.md and dispatch remediations"
      keywords: ["audit", "claude.md", "single-file audit", "compliance verdict", "dispatch"]
      goal: "For each target CLAUDE.md, run mechanical and judgment-based checks against the framework's contract, classify findings into the taxonomy, dispatch remediations to AUTO/DISCUSS/SPECIAL buckets, and emit a per-file compliance verdict."
      preconditions:
        - "audit.py is reachable (mechanical schema validator -- only needed if a claude_md: YAML block is present)."
        - "references/audit-criteria.md is loadable (the self-contained classic criteria doc; the upstream cohesion-principles is its derivation and is NOT loaded by the audit path)."
        - "references/code-dir-insight-filter.md is loadable -- needed only when a target is flagged dimension=code-directory; the self-contained CD-1..CD-6 criteria, anchor-modality table, and value filter."
        - "references/density-criteria.md is loadable -- needed only when the density lens was requested (the `density` arg or equivalent intent); the self-contained DD-1..DD-4 criteria and the density-not-deletion rule."
        - "The user is in a project directory so role classification works."
      steps:
        - n: 1
          action: "Resolve the audit target set from $ARGUMENTS. Empty -> cwd/CLAUDE.md. 'list' -> emit numbered list via discover.py and stop. Integers -> map to paths from last list. Path -> use directly. Strip any non-interactive token ('fast', '--fast', '--yes', '-y') from the args first and set non_interactive accordingly (also set it if the user's prose expresses non-interactive intent, e.g. 'just apply everything, don't ask'). Strip the density-lens token ('density', '--density') and set density_lens=true (also set it if the user's prose expresses the intent, e.g. 'is this too verbose', 'can anything move to a reference', 'audit for token efficiency'); density_lens is FALSE by default and the lens never runs unless requested. For each target capture (path, role, dimension, parentPath) where role is root / ancestor / child / local, dimension is the `code-directory`|`classic` flag discover.py emits (Level-1 trigger), and parentPath is the nearest ancestor CLAUDE.md for a child (else null)."
          tool: "discover.py"
          input: "uv run python ${CLAUDE_PLUGIN_ROOT}/skills/claude-md-audit/scripts/discover.py [--json]"
          expected: "Resolved (path, role, dimension, parentPath) tuples + non_interactive flag."
          on_failure: "If no CLAUDE.md resolves, surface cwd and stop. If a path is given directly (not via discover.py), classify its dimension by reading scripts/discover.py::classify_dimension semantics or default to `code-directory` when the file has code/yaml/csv siblings or review-claim markers and no `claude_md:` block."
        - n: 2
          action: "DETECT phase (before-Q&A). Choose execution mode by file count -- this threshold equalizes the Workflow tool's per-run overhead. ONE file: audit inline in the main loop (read the file + its parent if child; Read references/audit-criteria.md -- the single self-contained criteria doc, which states each testable rule with its CCP/CRP/ADP derivation inline; do NOT also load cohesion-principles; apply the role-to-criteria map; if a `claude_md:` block is present run the schema validator; if dimension=code-directory ALSO Read references/code-dir-insight-filter.md and run the CD-1..CD-6 insight-validation dimension -- classify each anchor's modality first, then fidelity/line/claim/value; for dimension=classic do NOT load the filter; if density_lens is TRUE ALSO Read references/density-criteria.md and run the DD-1..DD-4 density lens -- emit findings under group Density, all JUDGMENT/DISCUSS, never FAIL/AUTO, each remediation naming where the tokens go; if density_lens is FALSE do NOT load that doc; classify each finding into taxonomy + bucket). TWO OR MORE files: call the Workflow tool with scriptPath ${CLAUDE_PLUGIN_ROOT}/skills/claude-md-audit/workflow/detect.js and args = { files:[{path,role,dimension,parentPath}], density:<density_lens bool>, refs:{criteria, codeDirFilter, densityCriteria, pluginRoot, venvPython} }. The workflow fans one lane out per file and returns { perFile:[...], totals }. Detection only -- no file is edited in this phase."
          tool: "Workflow | inline"
          input: "detect.js args.refs: criteria=${CLAUDE_PLUGIN_ROOT}/skills/claude-md-audit/references/audit-criteria.md; codeDirFilter=${CLAUDE_PLUGIN_ROOT}/skills/claude-md-audit/references/code-dir-insight-filter.md; densityCriteria=${CLAUDE_PLUGIN_ROOT}/skills/claude-md-audit/references/density-criteria.md (passed only when args.density is true); pluginRoot=${CLAUDE_PLUGIN_ROOT}; venvPython=<plugin venv python>. Each file carries its dimension; the lane loads the code-dir filter only for dimension=code-directory and the density criteria only when args.density is true. (cohesion-principles is intentionally NOT passed -- lanes load only the self-contained criteria doc(s) for cache efficiency.) Schema validator is run as: (cd ${CLAUDE_PLUGIN_ROOT} && <venvPython> -m skills_kit_lib.audit <path> --json)."
          expected: "Structured per-file findings (group incl. CodeDir and Density, severity, criterion, message, line, taxonomy, bucket, remediation) + per-file verdict."
          on_failure: "If the schema validator is unavailable, the lane marks the Schema group JUDGMENT ('validator unavailable') and continues -- never fail a file for that. If an anchor cannot be classified or resolved cheaply, mark it external-unverifiable/INFO rather than FAIL."
        - n: 3
          action: "Render the per-file report (output_template) from the collected findings: per-file verdict blocks followed by an overall summary with bucket counts. This is the before-Q&A surface the user reads."
          expected: "Markdown report in the user's chat with compliance verdicts and AUTO/DISCUSS/SPECIAL counts."
        - n: 4
          action: "Q&A GATE. If non_interactive is FALSE (default): for each DISCUSS and SPECIAL finding, ask the user for a decision (apply as-proposed / skip / a refined instruction). Surface one decision at a time or a tight grouped set; do not dump a giant list. If non_interactive is TRUE: infer each decision from the taxonomy's default_remediation plus the file content and proceed WITHOUT prompting -- record each inferred decision in the final summary so the user can see and reverse them. AUTO findings need no decision (they apply by definition)."
          expected: "A decision (explicit or inferred) attached to every AUTO/DISCUSS/SPECIAL finding."
        - n: 5
          action: "REMEDIATE phase (after-Q&A). Assemble per-file remediation lists from the decided findings (AUTO=apply; DISCUSS/SPECIAL=per decision; drop skips). Choose mode by how many FILES carry remediation work. ONE file: apply inline with Edit. TWO OR MORE files: call the Workflow tool with scriptPath ${CLAUDE_PLUGIN_ROOT}/skills/claude-md-audit/workflow/remediate.js and args = { perFile:[{path,role,remediations:[{criterion,taxonomy,bucket,line,instruction,decision}]}] }. One lane per file (disjoint files never conflict)."
          tool: "Workflow | inline"
          expected: "Edits applied to the target files; per-file applied/skipped/failed summary."
        - n: 6
          action: "Render the final summary: what was applied per file, what was skipped, any failures, and the bucket totals. Remind the user that re-running the audit should now reproduce a clean (or reduced-FAIL) verdict -- detection and remediation are separate passes, so the re-run is the verification step."
          expected: "Closing summary; user can re-run /claude-md-audit to verify FAILs cleared."
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
        Remediation routed: AUTO=<N>, DISCUSS=<N>, SPECIAL=<N>
      gotchas:
        - "Role classification is anchored on cwd (the directory claude was launched in). The cwd CLAUDE.md is `root` only when no CLAUDE.md exists above it; if an ancestor CLAUDE.md is found, the cwd file is classified `child` so the project-root-only hygiene checks (H1/H2/H3) do not fire on a subordinate file and the parent-child duplication check runs against the ancestor."
        - "INFO findings are advisory (size signals, migration opportunities). They do NOT escalate to FAIL on subsequent runs even if unaddressed."
        - "When auditing a child CLAUDE.md, the parent must be read for CCP duplication checks. If the parent cannot be located (e.g. standalone file with no project context), report 'parent unavailable' for parent-relative criteria rather than failing them silently."
        - "For role=local (CLAUDE.local.md), only D-group criteria apply (see role-to-criteria map). Hygiene and ADP rules are skipped because the file is by design personal-scoped."
        - "The density lens (DD-1..DD-4, group Density) is OPT-IN and ADVISORY: it loads references/density-criteria.md only when density_lens is true, every finding is JUDGMENT/DISCUSS, and it never produces FAIL or AUTO. A density-only run is always COMPLIANT -- the lens surfaces token-efficiency opportunities, it never gates. Density findings do not change the verdict and do not escalate on re-runs."
        - "Density vs the classic criteria -- do not double-count: O_low_value_verbose (DD-4) is the CLASSIC-file value filter and must not fire on a code-directory file (CD-5/J owns value there); M_extract_to_reference (DD-2) is a disclosure-level move within one scope, distinct from A_wrong_role_content (different file) and finer than C_crp_split_candidate (whole-file split); N_intra_file_redundancy (DD-3) is within one file, distinct from B (across the role chain, FAIL/AUTO)."
  remediations:
    auto:
      - category: "B_ccp_cross_file_duplication"
        procedure: "Delete the restated rule from the child file. The parent rule loads automatically when the agent descends into the child directory."
        agent_template: "Background agent receives child CLAUDE.md path + duplicated-rule line range + parent rule reference. Applies the deletion and confirms the parent rule is still present."
      - category: "I2_line_drift"
        procedure: "Remove the stale line number from the claim, keeping the symbol anchor. The symbol resolves; only the number rotted."
        agent_template: "Background agent receives the claim line + the cited (drifted) line number + the resolved symbol location. Strips the number, leaves the symbol reference intact."
    discuss:
      - category: "H_stale_anchor"
        procedure: "Surface the unresolved requires-present anchor. Re-anchor to the current symbol/path the user identifies, or delete the claim if the code is gone."
      - category: "H2_inverted_absence"
        procedure: "Surface the violated invariant (the asserted-absent thing is now present). The remediation is in the repo, not the CLAUDE.md; confirm whether the claim should escalate to a code-review finding."
      - category: "I_claim_drift"
        procedure: "Show the claim and the contradicting code read. User updates the mechanism/magnitude or retires the claim. Counted magnitudes alone are never flagged."
      - category: "J_low_value_insight"
        procedure: "Show the low-value section and why it fails the value filter (after carve-outs). User confirms deletion or downgrade; a genuinely bare inventory may be deleted as AUTO."
      - category: "A_wrong_role_content"
        procedure: "Propose moving the misplaced section to the correct-scope CLAUDE.md. Show the destination and the line range to move. User confirms before applying."
      - category: "C_crp_split_candidate"
        procedure: "Propose an L1 -> L2/L3 decomposition: which sections move to a SKILL.md, which become reference docs, and the triggering criteria per reference. User confirms before splitting."
      - category: "D_adp_forward_dependency"
        procedure: "Surface the forward reference. Ask user: inline the descendant content (rule is parent-scoped) or remove the reference (rule is descendant-scoped)? Apply the user's choice."
      - category: "E_schema_failure"
        procedure: "Show the failing schema rows. AUTO sub-cases (missing optional defaults) can be applied immediately; authorial-choice rows wait for the user."
      - category: "F_hygiene_threshold"
        procedure: "Run the CRP test (do body sections serve different reading tasks?). If yes, escalate to C. If no, INFO stays; the larger CLAUDE.md is correct."
      - category: "G_descendant_role_mismatch"
        procedure: "Propose moving project-conventional content from .local file into the checked-in CLAUDE.md (so all collaborators see it). User confirms before applying."
      - category: "L_verbose_in_place"
        procedure: "Show the over-worded section and a tightened rewrite (or the sentences to compress) with an approximate token-savings figure. Tighten IN PLACE only -- never move or delete. User confirms; honor carve-outs (teaching examples, load-bearing nuance, labeled safety-rail repetition)."
      - category: "M_extract_to_reference"
        procedure: "Propose moving the self-contained on-demand block to a named reference doc (or a SKILL.md for on-task procedure), leaving a one-line pointer behind, with an approximate token-savings figure. Disclosure-level move within the same scope; user confirms before extracting."
      - category: "N_intra_file_redundancy"
        procedure: "Show the repeated statements; propose keeping the single best one and cross-referencing the others. User confirms which survives."
      - category: "O_low_value_verbose"
        procedure: "Show the section and why it fails the value lattice (code-dir-insight-filter.md Step 4) after carve-outs. Propose downgrade-to-a-line or, for a contentless section, deletion. User confirms; this lens never auto-deletes."
    special:
      procedure: "Surface the finding with the audit row that fired, attempted categories, and reasons none fit. User proposes strategy. Generalizable strategies become new taxonomy categories in references/audit-criteria.md."
  enforcement:
    gate_kind: "audit-finding"
    gating_rule: "FAIL findings (CCP cross-file duplication, ADP forward dependency, schema validation failures with non-optional missing fields) gate compliance. JUDGMENT findings surface for review without gating; INFO findings are advisory only."
    appeal_process: "JUDGMENT findings are resolved by user confirmation (PASS once the user accepts the exception explicitly). FAIL findings have no bypass; remediation is available within the taxonomy."
  gotchas:
    - "The subject is a corpus of CLAUDE.md files, but the audit procedure visits one file at a time. The role-to-criteria map ensures the right criteria apply to the right file."
    - "Role classification is cwd-relative. A standalone audit of a single file outside a project tree will classify it as root by default; surface that assumption if it affects criteria."
    - "Schema validation is conditional -- only files carrying a `claude_md:` YAML contract block are checked. Files without the block are not failed for its absence; CLAUDE.md is not required to declare a contract."
    - "Idempotency: criteria, taxonomy, and bucket assignments are fixed. Same input produces the same verdict; do not re-rank or re-order findings session-to-session."
  anti_patterns:
    - id: "audit_then_self_remediate"
      name: "Audit and remediate in the same procedure pass"
      keywords: ["self-remediation", "single-pass", "idempotency"]
      why_it_seems_right: "Auditing one file and applying remediations in the same pass seems efficient -- one tool call, fewer round trips."
      why_it_is_wrong: "Mixing detection and remediation in one pass breaks idempotency. The verdict and remediation are separate phases; conflating them prevents re-runs from producing the same findings."
      alternative: "Run the audit procedure to completion. Render the verdict. Dispatch remediations as separate AUTO + DISCUSS work units. Re-run the audit after remediation to verify."
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
- `fast` / `--fast` / `--yes` / `-y` -- non-interactive: skip the Q&A round and infer every DISCUSS/SPECIAL decision (see Non-interactive mode). Combine with any selector, e.g. `/claude-md-audit 3 7 fast`. Prose intent ("audit these and just apply everything, don't ask me") sets the same flag.
- `density` / `--density` -- add the opt-in density lens (DD-1..DD-4: verbosity-in-place, extract-to-reference, intra-file redundancy, value-earns-tokens). Advisory only -- all findings are JUDGMENT/DISCUSS, never FAIL/AUTO, so a density-only run is always COMPLIANT. Combine with any selector, e.g. `/claude-md-audit 3 density`. Prose intent ("is this CLAUDE.md too verbose", "can anything move to a reference", "audit for token efficiency") sets the same flag. Off by default; the lens never runs unless requested.

Typical workflow: `/claude-md-audit list` to see what's available, then `/claude-md-audit 3 7` to audit specific files. Add `density` to also surface token-efficiency opportunities: `/claude-md-audit 3 density`.

## Workflow orchestration

This skill runs in two phases split by an interactive Q&A gate, and uses the Workflow tool to fan the work out across files. **Invoking this skill authorizes the Workflow-tool calls described below** (the skill's instructions are the opt-in; do not re-prompt the user for permission to orchestrate).

```
resolve (main loop)
  -> DETECT  (before-Q&A)  : 1 file inline | 2+ files via workflow/detect.js   -> structured findings
  -> render report (main loop)
  -> Q&A GATE (main loop)  : interactive decisions | inferred when non-interactive
  -> REMEDIATE (after-Q&A) : 1 file inline | 2+ files via workflow/remediate.js -> edits applied
  -> final summary + "re-run to verify"
```

**Multi-file threshold (the overhead equalizer).** The Workflow tool has real per-run overhead (background orchestration, agent spin-up). For a single file that overhead is not worth it, so a 1-file audit runs inline in the main loop. At 2+ files the parallel fan-out pays for itself, so detection (and, separately, remediation) go through the workflow scripts. Detection and remediation are **always separate passes** even in workflow mode -- the interactive Q&A sits between them, and a background workflow cannot ask the user anything. This split is also what preserves the `audit_then_self_remediate` anti-pattern: re-running the audit reproduces the same findings because nothing was remediated during detection.

**The two workflow scripts** (hand-authored, shipped as skill assets):

- `workflow/detect.js` -- before-Q&A. One lane per file: read (+parent if child) -> apply criteria -> schema-validate -> classify. Returns `{ perFile, totals }`. No edits.
- `workflow/remediate.js` -- after-Q&A. One lane per file (disjoint files, no conflicts): apply the decided edits. Returns `{ perFile, summary }`.

Both accept `args` as an object or JSON string. Pass absolute `refs` paths (they run from the session cwd, not the skill dir).

## Non-interactive mode

When the non-interactive flag is set (argument token or expressed intent), the Q&A gate does not prompt. Instead, infer each DISCUSS/SPECIAL decision from the taxonomy's `default_remediation` plus the file content, apply them, and **list every inferred decision in the final summary** so the user can see and reverse them. AUTO findings apply regardless. FAIL findings are still gated by the verdict; non-interactive only changes how the *decisions* are obtained, not the audit contract. Interactive mode is the default; non-interactive is opt-in per the rule above.

## Decision rules

- Any FAIL finding -> file is NON-COMPLIANT.
- Only PASS / INFO / JUDGMENT findings -> file is COMPLIANT.
- INFO findings are advisory improvements, not compliance failures, and do not escalate to FAIL on subsequent runs.

## Cross-references

- Canonical placement framework: `cohesion-principles (in skills-kit)`. The criteria in this skill's `references/audit-criteria.md` derive directly from that skill's content_allocation framework; when the two diverge, the canonical framework wins.
- Schema validation tooling: `plugins/skills-kit/skills/skill-authoring/scripts/audit.py` (validates `claude_md:` YAML blocks against `CLAUDE_MD_SCHEMA` in schemas.py).
- Code-directory insight-validation criteria: `references/code-dir-insight-filter.md` (the self-contained CD-1..CD-6 dimension, loaded only for dimension=code-directory files). The Level-1 trigger that flags the dimension lives in `scripts/discover.py::classify_dimension`.
- Density lens criteria: `references/density-criteria.md` (the self-contained DD-1..DD-4 opt-in lens for verbosity/disclosure, loaded only when the `density` arg or equivalent intent is given). It reuses the value lattice in `references/code-dir-insight-filter.md` Step 4 rather than restating it. Advisory only -- never gates compliance.
- Authoring counterpart: `claude-md-authoring:references/code-directory-claude-md.md` -- authoring code-directory CLAUDE.md files to that doc is what keeps this audit green (same four shapes, observation taxonomy, anchoring + path discipline).
- Sibling audit skills: `/skill-audit` for SKILL.md files; `/references-audit` for broken cross-references across markdown.
