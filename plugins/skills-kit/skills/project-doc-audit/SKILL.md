---
_schema_version: 1
name: project-doc-audit
author: christina
skill-type: audit-skill
description: Use when md-audit dispatches a project-document audit against the cohesion framework. Do NOT use for SKILL.md, CLAUDE.md, or cross-references.
disable-model-invocation: true
user-invocable: false
argument-hint: "[file/dir path, number(s) from list, or 'list'; add 'fast' for non-interactive]"
---

# Project Document Audit

## Plugin version (always echo first)

!`uv run python "${CLAUDE_PLUGIN_ROOT}/scripts/print_version.py"`

The first line of your response MUST be the `Running ...` line printed above. This gives the user immediate confirmation of which plugin version actually executed (the slash registry can lag the on-disk cache; this is the only reliable signal).

Audit a **project document** -- a standalone reference doc that is NOT a SKILL.md, NOT a CLAUDE.md, and NOT inside a skill's `references/` folder (e.g. `Docs/*.md`, `Docs/**/*.md.html`, `.claude/docs/*.md`, `<subsystem>/docs/*.md`, READMEs, design notes, hand-off plans) -- against the cohesion-principles `project_reference_md` role and the skill-maturation pipeline. Findings are grouped by principle: Placement (maturation / home), CRP (single reading task), ADP (load-graph direction + discoverability), CCP (no duplication of skill content), plus universal hygiene.

The audit is idempotent: same input produces the same findings; addressing all FAIL findings produces a COMPLIANT verdict on the next run.

```yaml
audit_skill:
  _schema_version: "1"
  identity: "Audit project documents (standalone reference docs outside any skill's references/ and outside the CLAUDE.md hierarchy) against the cohesion-principles project_reference_md role + the skill-maturation pipeline. Classify findings into a taxonomy and dispatch remediations by bucket."
  scope:
    covers:
      - "auditing a standalone project document (Docs/, .claude/docs/, <subsystem>/docs/, README/design/plan docs) against the cohesion-principles project_reference_md role"
      - "the skill-maturation judgment -- whether matured content should graduate to a skill, fold into a CLAUDE.md, or move into an existing skill's references/ (prefer_skill_reference + skill_maturation_pipeline)"
      - "discoverability / orphan detection -- whether the doc is reachable from a CLAUDE.md, SKILL.md, or sibling doc in the load graph (mechanical inbound-citation count from discover.py)"
      - "CRP single-reading-task and one-hop-deep cross-reference checks; ADP no-back-reference-into-CLAUDE.md; CCP no-duplication-of-skill-content"
      - "listing project documents visible from a scan root (the discover.py helper for index-based selection)"
    excludes:
      - "auditing SKILL.md files (use /md-audit skill)"
      - "auditing CLAUDE.md / CLAUDE.local.md files (use /md-audit claude-md)"
      - "auditing skill-attached reference docs (*/skills/*/references/*.md -- audited transitively via their SKILL.md by /md-audit skill)"
      - "auditing cross-references / broken skill links across markdown (use /md-audit references)"
      - "imposing any specific project's documentation-home layout -- this audit enforces the generic cohesion framework, not a per-project directory policy (that belongs in the project's own CLAUDE.md)"
  subject:
    what: "Standalone project documents (plain_md / Markdeep .md.html / .txt) that sit outside any skill's references/ folder and outside the CLAUDE.md hierarchy, evaluated against the cohesion-principles project_reference_md role and the skill-maturation pipeline."
    subject_type: "corpus"
  criteria:
    - id: "placement_not_in_skill_dir"
      name: "Placement -- file is a genuine project doc, not a mis-placed skill reference"
      keywords: ["placement", "skill reference", "misclassified", "references folder", "project doc"]
      summary: "A project document lives at a project-level path, NOT inside a `*/skills/*/references/` folder. A doc found inside a skill's references/ is a skill reference (audited via /md-audit skill), not a project doc -- discover.py classifies it `skill_reference` and the audit skips it."
      severity: "INFO"
      detail: "Mechanical, from discover.py `kind`. Classification guard, not a finding against the file -- it routes the file to the right auditor. Surfaced INFO when a selected target is actually a skill_reference."
    - id: "placement_maturation"
      name: "Placement -- content has matured past a project doc (graduate / fold / absorb)"
      keywords: ["maturation", "graduate to skill", "prefer skill reference", "nursery", "escape hatch", "fold into claude.md"]
      summary: "Project references are the escape-hatch / nursery, not the default home for reference content. A doc whose content has stabilized into a skill-typed shape (procedure -> technique; rule+counter -> discipline; lookup table -> reference; tool/API wrapper -> capability) should graduate to a skill. A small load-bearing tip should fold into a CLAUDE.md. Content owned by an existing skill's topic should move into that skill's references/."
      severity: "JUDGMENT"
      detail: "Judgment call from cohesion-principles prefer_skill_reference + skill_maturation_pipeline. The lane reads the doc and asks: does this content's shape fit a skill type, a CLAUDE.md insight, or an existing skill? INFO/JUDGMENT only -- a project doc doing useful work where it sits is never a FAIL."
    - id: "adp_discoverability"
      name: "ADP -- the doc is reachable in the load graph (not an orphan)"
      keywords: ["orphan", "discoverability", "inbound citation", "unreachable", "dangling doc", "load graph"]
      summary: "A project document is loaded on demand when a CLAUDE.md / SKILL.md / sibling doc cites it by name. A doc with zero inbound citations is an orphan: nothing in the agent load graph points to it, so it never loads. Either add a pointer from the owning CLAUDE.md or retire the doc."
      severity: "JUDGMENT"
      detail: "Mechanical orphan signal from discover.py `inbound_citations == 0`; the JUDGMENT is whether the orphan is intentional (human-only doc, e.g. a published design record) or dead weight. Never auto-FAIL -- a doc can legitimately serve human readers outside the agent load graph."
    - id: "crp_unitary_reading_task"
      name: "CRP -- the doc serves a single reading task"
      keywords: ["crp", "single reading task", "split candidate", "multi-trigger", "decomposition"]
      summary: "Every reader who lands on a project doc should need all of it. A doc that bundles content firing on different sub-triggers (a setup guide + an API table + a troubleshooting log) serves multiple reading tasks and should split, each part landing at the scope whose readers all need it."
      severity: "JUDGMENT"
      detail: "Judgment call per cohesion-principles project_reference_md.crp_unitary_reading_task. Size (from discover.py) is a SIGNAL that prompts the evaluation, never a verdict. Split only when a CRP-passing decomposition exists."
    - id: "adp_one_hop_deep"
      name: "ADP -- cross-references to sibling docs are one hop, not chained"
      keywords: ["adp", "one hop", "chained reference", "reference chain", "transitive"]
      summary: "A project doc may cite a sibling project doc or a SKILL.md by name (informational pointer), but cross-reference chains (A -> B -> C as a required reading path) are prohibited -- readers tend to stop at the second hop."
      severity: "FAIL"
      detail: "Detected by scanning outbound doc-to-doc citations and checking the cited doc does not itself require following a further citation to be understood. Same one-hop rule as skill references."
    - id: "adp_no_claude_md_back_reference"
      name: "ADP -- no citation of CLAUDE.md sections by name"
      keywords: ["adp", "back reference", "claude.md citation", "load order", "reverse edge"]
      summary: "A project doc is loaded AFTER the CLAUDE.md that cites it. Citing CLAUDE.md sections by name reverses the load-order direction. The doc may name the CLAUDE.md as an orientation surface but must not depend on CLAUDE.md content the reader has already passed."
      severity: "FAIL"
      detail: "Detected by scanning the body for `CLAUDE.md` mentions that cite specific section content as a dependency. Pure orientation mentions ('see the root CLAUDE.md for project setup') are permitted."
    - id: "ccp_no_skill_content_duplication"
      name: "CCP -- the doc does not duplicate content already owned by a skill"
      keywords: ["ccp", "ssot", "duplication", "skill content", "parallel reference", "collapse to pointer"]
      summary: "When a skill exists for the doc's topic, that skill's references/ is the SSOT. A project doc that restates skill content creates a second copy that drifts. The doc should collapse to a pointer ('for X, invoke /skill-name')."
      severity: "FAIL"
      detail: "Judgment-assisted: the lane checks whether a skill covers the doc's topic and whether the doc restates (rather than points at) that skill's content. INFO when the project ref predates the skill and graduation is in progress; FAIL on live parallel duplication."
    - id: "hygiene_thresholds"
      name: "Hygiene -- size signal and broken outbound file links"
      keywords: ["hygiene", "size signal", "broken link", "file path", "line count"]
      summary: "Body length is a CRP-evaluation signal (over ~500 lines / 3000 tokens prompts the unitary-reading-task check). Outbound file-path references must resolve. Broken SKILL-link / cross-reference integrity is delegated to /md-audit references, not re-checked here."
      severity: "INFO"
      detail: "Mechanical: line/token count (from discover.py) and file-path resolution. Cross-reference (skill-link) integrity is out of scope -- references-audit owns it."
  taxonomy:
    - id: "A_misclassified_skill_ref"
      name: "Selected target is actually a skill reference"
      keywords: ["skill reference", "wrong auditor", "references folder", "misrouted"]
      detection_signal: "discover.py classified the path `skill_reference` (it sits inside a `*/skills/*/references/` folder)."
      default_remediation: "Skip the file in this audit and note it is covered by /md-audit skill (the SKILL.md that owns the references/ folder audits it transitively). No edit to the file."
      bucket: "DISCUSS"
    - id: "B_graduate_to_skill"
      name: "Matured content should graduate to a skill"
      keywords: ["graduate", "skill type", "technique", "discipline", "reference skill", "capability", "maturation"]
      detection_signal: "The doc's content has stabilized into a recognizable skill-typed shape (procedure / rule+counter / lookup table / tool wrapper) with a clear trigger -- the graduation_signal in the skill_maturation_pipeline."
      default_remediation: "Propose graduating the doc into a skill of the matching type: name the type, the trigger, and whether the doc content becomes structured SKILL.md content or moves into the skill's references/. User confirms (this is a new-skill authoring task -- /md-authoring skill)."
      bucket: "DISCUSS"
    - id: "C_fold_into_claude_md"
      name: "Small load-bearing content should fold into a CLAUDE.md"
      keywords: ["fold", "inline", "claude.md insight", "small tip", "guardrail"]
      detection_signal: "The doc is small (a tip, a single fact, a one-line guardrail) and load-bearing in a way that justifies ambient cost -- stage 1 of the maturation pipeline, currently over-promoted to a standalone doc."
      default_remediation: "Propose folding the content into the owning CLAUDE.md (as an insight or convention) and deleting the standalone doc. User confirms the destination CLAUDE.md."
      bucket: "DISCUSS"
    - id: "D_move_into_existing_skill"
      name: "Content belongs in an existing skill's references/"
      keywords: ["move to skill", "existing skill", "references folder", "wrong home"]
      detection_signal: "An existing skill already owns the doc's topic; the content belongs inside that skill's references/ rather than as a parallel project doc."
      default_remediation: "Propose moving the doc into the owning skill's references/ folder and leaving a pointer (or nothing) behind. User confirms the target skill."
      bucket: "DISCUSS"
    - id: "E_crp_split"
      name: "Doc serves multiple reading tasks (CRP split warranted)"
      keywords: ["crp split", "multiple reading tasks", "decomposition", "unitary"]
      detection_signal: "Over the size signal AND the lane judges sections serve genuinely different sub-triggers (setup + reference table + troubleshooting)."
      default_remediation: "Propose a CRP decomposition: which sections split out, and the destination scope for each (a skill, a CLAUDE.md, or a separate sibling doc). User confirms before splitting."
      bucket: "DISCUSS"
    - id: "F_chained_reference"
      name: "Cross-reference chain deeper than one hop"
      keywords: ["chained reference", "one hop", "transitive reference", "reference chain"]
      detection_signal: "The doc requires following a citation to a sibling doc that itself requires following a further citation (A -> B -> C reading path)."
      default_remediation: "Flatten to one hop: inline the second-hop content the reader needs, or restructure so each doc is understandable after one citation. User confirms."
      bucket: "DISCUSS"
    - id: "G_claude_md_back_reference"
      name: "Doc cites CLAUDE.md section content as a dependency"
      keywords: ["back reference", "claude.md citation", "reverse edge", "load order"]
      detection_signal: "The body cites specific CLAUDE.md section content as required reading (reversing load order), beyond a permitted orientation mention."
      default_remediation: "Remove the back-citation, or inline the small fact the doc actually needs. Keep orientation mentions ('see the root CLAUDE.md') that do not depend on CLAUDE.md content. User confirms."
      bucket: "DISCUSS"
    - id: "H_orphan"
      name: "Orphan -- nothing in the load graph points at the doc"
      keywords: ["orphan", "no inbound citation", "unreachable", "dangling", "discoverability"]
      detection_signal: "discover.py reports inbound_citations == 0 -- no CLAUDE.md / SKILL.md / sibling doc references the file by name."
      default_remediation: "Either add a one-line pointer from the owning CLAUDE.md (so the doc loads on demand for agents) or, if the doc is dead, retire it. If the doc is intentionally human-only (a published design record), confirm and mark it as accepted (PASS). User decides."
      bucket: "DISCUSS"
    - id: "I_duplicates_skill"
      name: "Doc duplicates content already owned by a skill"
      keywords: ["duplication", "ssot", "parallel reference", "skill content", "collapse to pointer"]
      detection_signal: "A skill covers the doc's topic and the doc restates (rather than points at) that skill's content."
      default_remediation: "Collapse the doc to a pointer ('for X, invoke /skill-name') so the skill's references/ stays the SSOT. User confirms before deleting the duplicated content."
      bucket: "DISCUSS"
    - id: "J_size_signal"
      name: "Body over size threshold (CRP-evaluation prompt)"
      keywords: ["size signal", "line count", "token count", "crp evaluation"]
      detection_signal: "Mechanical INFO: effective lines > 500 or approx tokens > 3000."
      default_remediation: "Run the CRP test (do sections serve different reading tasks?). If yes, escalate to E. If no, INFO stays -- a large single-task doc is correct."
      bucket: "DISCUSS"
    - id: "K_unclassified"
      name: "Unclassified / special case"
      keywords: ["unclassified", "special case", "escape hatch", "K bucket"]
      detection_signal: "Finding does not match any A-J detection signal after a deliberate attempt."
      default_remediation: "Surface to the user with the audit row that fired, attempted matches, and reasons none fit. User proposes strategy."
      bucket: "SPECIAL"
  procedures:
    - id: "audit_project_doc"
      name: "Audit project documents and dispatch remediations"
      keywords: ["audit", "project doc", "single-file audit", "compliance verdict", "dispatch"]
      goal: "For each target project document, run mechanical and judgment-based checks against the cohesion-principles project_reference_md role + maturation pipeline, classify findings into the taxonomy, dispatch remediations to AUTO/DISCUSS/SPECIAL buckets, and emit a per-file compliance verdict."
      preconditions:
        - "discover.py is reachable (enumerates candidate project docs + the mechanical orphan/size signals)."
        - "references/audit-criteria.md is loadable (the self-contained criteria doc; the upstream cohesion-principles is its derivation and is NOT loaded by the audit path)."
        - "The user is in a project directory so discoverability / orphan signals are meaningful."
      steps:
        - n: 1
          action: "Resolve the audit target set from $ARGUMENTS. Empty -> scan cwd and list. 'list' -> emit numbered list via discover.py and stop. A directory path -> scan it for project docs (discover.py --root). A file path -> audit it directly (discover.py --path). Integers -> map to paths from the last list. Strip any non-interactive token ('fast', '--fast', '--yes', '-y') first and set non_interactive accordingly (also set it if the user's prose expresses non-interactive intent). For each target capture (path, kind, lines, approx_tokens, inbound_citations, cited_by) from discover.py --json. Drop targets whose kind is `skill_reference` or `other_claude_artifact` with an A_misclassified_skill_ref note (route them to the right auditor)."
          tool: "discover.py"
          input: "uv run python ${CLAUDE_PLUGIN_ROOT}/skills/project-doc-audit/scripts/discover.py [--root DIR | --path FILE ...] --json"
          expected: "Resolved per-doc records (path, kind, lines, approx_tokens, inbound_citations, cited_by) + non_interactive flag."
          on_failure: "If no project docs resolve, surface the scan root and stop."
        - n: 2
          action: "DETECT phase (before-Q&A). Choose execution mode by file count -- this threshold equalizes the Workflow tool's per-run overhead. ONE file: audit inline in the main loop (Read the doc; Read references/audit-criteria.md -- the single self-contained criteria doc; apply the project_reference_md criteria; use the mechanical signals discover.py already computed -- orphan from inbound_citations, size from lines/approx_tokens; judge maturation / CRP / duplication; classify each finding into taxonomy + bucket). TWO OR MORE files: call the Workflow tool with scriptPath ${CLAUDE_PLUGIN_ROOT}/skills/project-doc-audit/workflow/detect.js and args = { files:[{path, kind, lines, approx_tokens, inbound_citations, cited_by}], refs:{criteria, pluginRoot} }. The workflow fans one lane out per file and returns { perFile, totals }. Detection only -- no file is edited in this phase."
          tool: "Workflow | inline"
          input: "detect.js args.refs: criteria=${CLAUDE_PLUGIN_ROOT}/skills/project-doc-audit/references/audit-criteria.md; pluginRoot=${CLAUDE_PLUGIN_ROOT}. (cohesion-principles is intentionally NOT passed -- lanes load only the self-contained criteria doc for cache efficiency.)"
          expected: "Structured per-file findings (group, severity, criterion, message, line, taxonomy, bucket, remediation) + per-file verdict."
          on_failure: "If a maturation/duplication judgment cannot be made cheaply (e.g. the candidate skill is ambiguous), mark it JUDGMENT/DISCUSS rather than FAIL."
        - n: 3
          action: "Render the per-file report (output_template): per-file verdict blocks followed by an overall summary with bucket counts. This is the before-Q&A surface the user reads."
          expected: "Markdown report with compliance verdicts and AUTO/DISCUSS/SPECIAL counts."
        - n: 4
          action: "Q&A GATE. If non_interactive is FALSE (default): for each DISCUSS and SPECIAL finding, ask the user for a decision (apply as-proposed / skip / a refined instruction). Surface a tight grouped set; do not dump a giant list. If non_interactive is TRUE: infer each decision from the taxonomy's default_remediation plus the doc content and proceed WITHOUT prompting -- record each inferred decision in the final summary. AUTO findings need no decision."
          expected: "A decision (explicit or inferred) attached to every AUTO/DISCUSS/SPECIAL finding."
        - n: 5
          action: "REMEDIATE phase (after-Q&A). Assemble per-file remediation lists from the decided findings (AUTO=apply; DISCUSS/SPECIAL=per decision; drop skips). ONE file: apply inline with Edit. TWO OR MORE files: call the Workflow tool with scriptPath ${CLAUDE_PLUGIN_ROOT}/skills/project-doc-audit/workflow/remediate.js and args = { perFile:[{path, remediations:[{criterion, taxonomy, bucket, line, instruction, decision}]}] }. One lane per file (disjoint files never conflict). NOTE: graduation (B), fold-into-CLAUDE.md (C), and move-into-skill (D) remediations are multi-file structural moves -- the lane applies the move it is instructed to make; new-skill authoring beyond a simple move should be handed to /md-authoring skill rather than performed blind."
          tool: "Workflow | inline"
          expected: "Edits applied; per-file applied/skipped/failed summary."
        - n: 6
          action: "Render the final summary: what was applied per file, what was skipped, any failures, and the bucket totals. Remind the user that re-running the audit should reproduce a clean (or reduced-FAIL) verdict -- detection and remediation are separate passes, so the re-run is the verification step."
          expected: "Closing summary; user can re-run /md-audit project-doc to verify FAILs cleared."
      output_template: |
        ## <file path> (<kind>, <lines>L, <inbound_citations> inbound)

        Findings: <count by bucket>

        ### Placement (maturation / home)
        [PASS|INFO|JUDGMENT] <criterion>: <message>

        ### CRP (single reading task)
        [PASS|FAIL|JUDGMENT] <criterion>: <message>

        ### ADP (load-graph direction + discoverability)
        [PASS|FAIL|JUDGMENT] <criterion>: <message>

        ### CCP (no duplication of skill content)
        [PASS|FAIL|INFO] <criterion>: <message>

        ### Hygiene (universal)
        [PASS|INFO] <criterion>: <message>

        ### Compliance verdict

        <P> PASS / <F> FAIL / <I> INFO / <J> JUDGMENT-REQUIRED
        Verdict: COMPLIANT | NON-COMPLIANT
        Remediation routed: AUTO=<N>, DISCUSS=<N>, SPECIAL=<N>
      gotchas:
        - "discover.py classifies skill-attached references (*/skills/*/references/*.md) as `skill_reference` and CLAUDE.md/SKILL.md as `other_claude_artifact`. The audit only evaluates `project_doc`; a selected non-project-doc target produces a single A_misclassified_skill_ref INFO routing it to the right auditor."
        - "Orphan (H) is a JUDGMENT, never an auto-FAIL. A project doc can legitimately serve human readers outside the agent load graph (a published design record, a runbook a person opens). The audit surfaces the orphan; the user decides whether to add a pointer, retire it, or accept it."
        - "Maturation (B/C/D) findings are advisory -- a project doc doing useful work where it sits is COMPLIANT. Graduation is a higher-leverage opportunity, not a defect. Never FAIL a doc for not yet being a skill."
        - "Size is a SIGNAL (INFO/J), never a verdict. A large single-reading-task doc that passes CRP is correct; only escalate to E_crp_split when a CRP-passing decomposition genuinely exists."
        - "Cross-reference (broken skill-link) integrity is NOT this audit's job -- /md-audit references owns it. This audit checks doc-to-doc one-hop discipline and file-path resolution only."
      anti_patterns:
        - id: "audit_then_self_remediate"
          name: "Audit and remediate in the same procedure pass"
          keywords: ["self-remediation", "single-pass", "idempotency"]
          why_it_seems_right: "Auditing one doc and applying remediations in the same pass seems efficient -- one tool call, fewer round trips."
          why_it_is_wrong: "Mixing detection and remediation breaks idempotency. The verdict and remediation are separate phases; conflating them prevents re-runs from producing the same findings."
          alternative: "Run the audit to completion. Render the verdict. Dispatch remediations as separate AUTO + DISCUSS work units after the Q&A gate. Re-run the audit to verify."
        - id: "flag_every_orphan_for_deletion"
          name: "Treat every orphan as dead weight"
          keywords: ["orphan", "delete", "human-only doc", "false positive"]
          why_it_seems_right: "An orphan has zero inbound citations, so nothing loads it -- looks like dead weight to remove."
          why_it_is_wrong: "Many project docs serve human readers who open them directly (design records, runbooks, onboarding). Zero agent-load-graph citations does not mean zero value. Deleting them destroys human-facing documentation."
          alternative: "Surface the orphan as a JUDGMENT. Offer three paths: add a CLAUDE.md pointer (make it agent-reachable), retire it (genuinely dead), or accept it (intentionally human-only). The user picks."
  remediations:
    auto: []
    discuss:
      - category: "A_misclassified_skill_ref"
        procedure: "Note the file is a skill reference and is audited via /md-audit skill (its owning SKILL.md). Skip it here; no edit."
      - category: "B_graduate_to_skill"
        procedure: "Propose the skill type + trigger and whether content becomes SKILL.md body or references/. Hand the actual authoring to /md-authoring skill. User confirms the graduation."
      - category: "C_fold_into_claude_md"
        procedure: "Propose folding the small load-bearing content into the owning CLAUDE.md and deleting the standalone doc. User confirms the destination CLAUDE.md."
      - category: "D_move_into_existing_skill"
        procedure: "Propose moving the doc into the owning skill's references/ folder. User confirms the target skill."
      - category: "E_crp_split"
        procedure: "Propose the decomposition: which sections split and the destination scope for each. User confirms before splitting."
      - category: "F_chained_reference"
        procedure: "Flatten the reference chain to one hop (inline the second-hop content the reader needs). User confirms."
      - category: "G_claude_md_back_reference"
        procedure: "Remove the back-citation or inline the small fact the doc needs; keep permitted orientation mentions. User confirms."
      - category: "H_orphan"
        procedure: "Offer: add a CLAUDE.md pointer (make agent-reachable), retire the doc (dead), or accept as human-only (PASS). User picks."
      - category: "I_duplicates_skill"
        procedure: "Collapse the doc to a pointer at the owning skill so the skill's references/ stays SSOT. User confirms before deleting duplicated content."
      - category: "J_size_signal"
        procedure: "Run the CRP test (do sections serve different reading tasks?). If yes, escalate to E. If no, INFO stays; the large single-task doc is correct."
    special:
      procedure: "Surface the finding with the audit row that fired, attempted categories, and reasons none fit. User proposes strategy. Generalizable strategies become new taxonomy categories in references/audit-criteria.md."
  enforcement:
    gate_kind: "audit-finding"
    gating_rule: "FAIL findings (CRP chained reference, ADP back-reference into CLAUDE.md, CCP live duplication of skill content) gate compliance. JUDGMENT findings (maturation, orphan, CRP split candidacy) surface for review without gating; INFO findings are advisory only."
    appeal_process: "JUDGMENT findings are resolved by user confirmation (PASS once the user accepts the exception explicitly -- e.g. an intentionally human-only orphan). FAIL findings have no bypass; remediation is available within the taxonomy."
  gotchas:
    - "The subject is a corpus of project documents, but the audit procedure visits one file at a time. discover.py is the corpus enumerator + mechanical-signal source."
    - "Idempotency: criteria, taxonomy, and bucket assignments are fixed. Same input produces the same verdict; do not re-rank session-to-session."
    - "Discoverability (orphan) depends on the scan root. A narrow --path run cannot see citers outside the default scan tree; discover.py always scans from the project root for inbound citations, so run it from the project top for a reliable orphan signal."
```

## Argument grammar

- `(none)` -- scan cwd for project docs and list them (does not audit; equivalent to `list`).
- `list` -- show a numbered list of project docs visible from the scan root; do not audit.
- `<dir>` -- audit every project doc under a directory (e.g. `/md-audit project-doc .claude/docs`).
- `<file>` -- audit a specific project doc.
- `<numbers>` -- audit docs by index from the most recent `list` output (e.g. `3 7 9`).
- `fast` / `--fast` / `--yes` / `-y` -- non-interactive: skip the Q&A round and infer every DISCUSS/SPECIAL decision. Combine with any selector. Prose intent ("audit these and just apply everything, don't ask") sets the same flag.

Typical workflow: `/md-audit project-doc .claude/docs` to audit a whole doc home, or `/md-audit project-doc list` then `/md-audit project-doc 3 7` for specific files.

## Workflow orchestration

This skill runs in two phases split by an interactive Q&A gate, and uses the Workflow tool to fan the work out across files. **Invoking this skill authorizes the Workflow-tool calls described below** (the skill's instructions are the opt-in; do not re-prompt for permission to orchestrate).

```
resolve (main loop, via discover.py)
  -> DETECT  (before-Q&A)  : 1 file inline | 2+ files via workflow/detect.js   -> structured findings
  -> render report (main loop)
  -> Q&A GATE (main loop)  : interactive decisions | inferred when non-interactive
  -> REMEDIATE (after-Q&A) : 1 file inline | 2+ files via workflow/remediate.js -> edits applied
  -> final summary + "re-run to verify"
```

**Multi-file threshold (the overhead equalizer).** The Workflow tool has real per-run overhead. For a single file that overhead is not worth it, so a 1-file audit runs inline in the main loop. At 2+ files the parallel fan-out pays for itself, so detection (and, separately, remediation) go through the workflow scripts. Detection and remediation are **always separate passes** even in workflow mode -- the interactive Q&A sits between them, and a background workflow cannot ask the user anything.

**The two workflow scripts** (the detect script is hand-authored; the remediate script is generated from `scripts/gen_workflow_js.py` and drift-checked):

- `workflow/detect.js` -- before-Q&A. One lane per file: read the doc -> apply the project_reference_md criteria with the mechanical signals -> classify. Returns `{ perFile, totals }`. No edits.
- `workflow/remediate.js` -- after-Q&A. One lane per file (disjoint files, no conflicts): apply the decided edits. Returns `{ perFile, summary }`.

Both accept `args` as an object or JSON string. Pass absolute `refs` paths (they run from the session cwd, not the skill dir).

## Non-interactive mode

When the non-interactive flag is set (argument token or expressed intent), the Q&A gate does not prompt. Instead, infer each DISCUSS/SPECIAL decision from the taxonomy's `default_remediation` plus the doc content, apply them, and **list every inferred decision in the final summary** so the user can see and reverse them. FAIL findings are still gated by the verdict; non-interactive only changes how the *decisions* are obtained. Interactive mode is the default.

## Decision rules

- Any FAIL finding -> file is NON-COMPLIANT.
- Only PASS / INFO / JUDGMENT findings -> file is COMPLIANT.
- INFO and JUDGMENT findings are advisory; they do not escalate to FAIL on subsequent runs even if unaddressed.

## Cross-references

- Canonical placement framework: `cohesion-principles` (in skills-kit). The criteria in this skill's `references/audit-criteria.md` derive directly from that skill's `project_reference_md` role + `skill_maturation_pipeline`; when the two diverge, the canonical framework wins.
- Sibling audit skills: `skill-audit` (via `/md-audit skill`) for SKILL.md files; `claude-md-audit` (via `/md-audit claude-md`) for CLAUDE.md files; `references-audit` (via `/md-audit references`) for broken skill cross-references across markdown.
- Authoring counterpart: when a finding recommends graduation (B), absorption (C/D), or a split (E), the actual authoring is handed to `/md-authoring` (skill or claude-md) -- this audit detects and routes; it does not author new skills blind.
- Framework registry: the `project_doc_audit` audit-kind in `md-audit/references/audit-framework.yaml` binds this skill's criteria to the `plain_md` primitive over the `directory` / `project` compositions.
