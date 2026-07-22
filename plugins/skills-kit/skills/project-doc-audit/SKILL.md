---
_schema_version: 1
name: project-doc-audit
author: christina
skill-type: audit-skill
description: Use when md-audit dispatches a project-document audit against the cohesion framework. Do NOT use for SKILL.md, CLAUDE.md, or cross-references.
disable-model-invocation: true
user-invocable: false
argument-hint: "[file/dir path, number(s) from list, or 'list'; add 'fast' for non-interactive, '--review' to gate a change]"
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
  identity: "Audit project documents (standalone reference docs outside any skill's references/ and outside the CLAUDE.md hierarchy) against the cohesion-principles project_reference_md role + the skill-maturation pipeline. Classify findings into a taxonomy and assign each a disposition (FIX / SERIOUS / IMPROVE / SILENT; K -> SPECIAL) instance-level -- placement/maturation ids stay IMPROVE, the mechanical convention checks (N-R) are FIX."
  scope:
    covers:
      - "auditing a standalone project document (Docs/, .claude/docs/, <subsystem>/docs/, README/design/plan docs) against the cohesion-principles project_reference_md role"
      - "the skill-maturation judgment -- whether matured content should graduate to a skill, fold into a CLAUDE.md, or move into an existing skill's references/ (prefer_skill_reference + skill_maturation_pipeline)"
      - "discoverability / orphan detection -- whether the doc is reachable from a CLAUDE.md, SKILL.md, or sibling doc in the load graph (mechanical inbound-citation count from discover.py)"
      - "CRP single-reading-task and one-hop-deep cross-reference checks; ADP no-back-reference-into-CLAUDE.md; CCP no-duplication-of-skill-content"
      - "listing project documents visible from a scan root (the discover.py helper for index-based selection)"
      - "named-role dispatch for READMEs (derived human brief -- readme_md role) and committed generated artifacts (provenance-only audit -- generated_artifact role)"
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
      summary: "Project references are the escape-hatch / nursery for still-emerging content, not a permanent home. When content stabilizes, route it to its TRIGGER-APPROPRIATE mature home (cohesion-principles placement_follows_trigger_shape): a TASK-shaped trigger (a verb the session performs) -> graduate to a skill (B); a LOCATION-shaped trigger (knowledge scoped to a directory) -> fold into / reference from that directory's CLAUDE.md (C), the PREFERRED home for directory-scoped knowledge since a CLAUDE.md auto-loads when files beneath it are touched; content an existing skill's topic owns -> move into that skill's references/ (D). Skills are NOT the default home for all mature reference content."
      severity: "JUDGMENT"
      detail: "Judgment call from cohesion-principles placement_follows_trigger_shape + skill_maturation_pipeline. The lane reads the doc and asks: what is the natural TRIGGER for this content -- a verb (task-shaped -> skill) or working under a directory (location-shaped -> directory CLAUDE.md)? Do NOT recommend skill-graduation for location-scoped knowledge. INFO/JUDGMENT only -- a project doc doing useful work where it sits is never a FAIL."
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
    - id: "readme_role"
      name: "Named role -- README is the derived human brief"
      keywords: ["readme", "derived brief", "human-facing", "stranded facts", "identity overlap"]
      summary: "A README (discover.py role_hint == readme) is judged under the cohesion-principles readme_md role: the agent-facing copy (CLAUDE.md / skill graph) is the SSOT, README is the derived brief. Identity/architecture overlap with root CLAUDE.md is tolerated at the identity-sentence grain (not a PD-8 duplication finding); maturation and orphan checks are skipped (a README is intentionally human-facing). FAIL when a command, convention, or schema in README is not also reachable through the CLAUDE.md / skill graph -- agents never load README."
      severity: "FAIL"
      detail: "Role dispatch is mechanical (role_hint from discover.py); the stranded-fact check is judgment-assisted -- for each command block / convention / schema in the README, verify the fact or its SSOT is reachable from a CLAUDE.md or skill surface. INFO when the tolerated overlap grows past the identity-sentence grain."
    - id: "generated_artifact_provenance"
      name: "Named role -- generated artifact, provenance-only audit"
      keywords: ["generated artifact", "provenance", "sidecar", "params.json", "generation marker", "exempt"]
      summary: "A committed generated output (discover.py generated == true, via a generation-record sidecar or an in-file marker in the first ~20 lines) is exempt from the authored-doc criteria (maturation, CRP split, orphan, duplication, size). The single applicable check: the generator or session provenance is named. A doc CLAIMED as generated with neither signal FAILs (unverifiable provenance)."
      severity: "FAIL"
      detail: "Mechanical from discover.py generated / generation_record. With a signal present: PASS, all other criteria skipped -- no exemption needs declaring by hand in the doc. Claimed-generated without a signal: taxonomy M; remediation is adding a machine-readable generation record (the <name>.params.json sidecar recipe is the proven shape) or an in-file marker naming the generator."
    - id: "hygiene_thresholds"
      name: "Hygiene -- size signal and broken outbound file links"
      keywords: ["hygiene", "size signal", "broken link", "file path", "line count"]
      summary: "Body length is a CRP-evaluation signal (over ~500 lines / 3000 tokens prompts the unitary-reading-task check). Outbound file-path references must resolve -- a broken file-path link is a FAIL (PD-H1) that gates compliance. Broken SKILL-link / cross-reference integrity is delegated to /md-audit references, not re-checked here."
      severity: "FAIL"
      detail: "Two mechanical checks (from discover.py). Size is an INFO-level signal (line/token count) -- never a verdict. Broken outbound file-path resolution is the FAIL half (PD-H1): a broken file-path reference gates compliance (matching the gating_rule enumeration below and references/audit-criteria.md PD-H1). Cross-reference (skill-link) integrity is out of scope -- references-audit owns it."
    - id: "mechanical_convention_hygiene"
      name: "Hygiene -- mechanical convention violations (FIX-eligible)"
      keywords: ["broken link identified target", "non-ascii", "foreign absolute path", "line drift", "stale anchor", "mechanical fix", "convention violation"]
      summary: "Mechanical, fact-or-convention-decidable defects that a reasonable owner would accept in CL review without discussion: a broken outbound link with an identifiable target; a non-ASCII look-alike where ASCII-only is the convention; a hardcoded foreign/machine-specific absolute path (or backslash path); a drifted cited line number; a stale anchor re-pointable to a found mechanism. These are the project-doc audit's FIX-eligible checks -- additive to the historically placement-only taxonomy."
      severity: "INFO"
      detail: "Each maps to a FIX taxonomy id (N broken-link-with-target, O non-ASCII, P foreign-abs-path / backslash, Q line-drift, R stale-anchor). Decidable by verified facts + documented conventions; the disposition classifier assigns FIX (or SERIOUS for a stale anchor guarding a rail with no surviving mechanism). A generator-owned path absent from the checkout is NOT a broken link -- annotating it auto-generated is FIX, repoint/delete is IMPROVE."
    - id: "ancestor_convention_conformance"
      name: "Hygiene -- doc conforms to conventions an ancestor CLAUDE.md explicitly declares (PD-11)"
      keywords: ["ancestor convention", "PD-11", "ascii-only", "no absolute paths", "declared convention", "verbatim rule", "exception aware"]
      summary: "A convention EXPLICITLY declared in an ancestor CLAUDE.md (ASCII-only, no absolute paths in shared files, stated formatting rules) loads ambient in any session touching this doc, so it binds the doc too. A subject that violates a verbatim-quotable ancestor rule is a FAIL (PD-11). Fires only when ancestorClaudeMdPaths is supplied and non-empty; a doc with no ancestor CLAUDE.md emits nothing. EXCEPTION-AWARE: an ancestor's explicitly declared scoped exception that covers the exact instance suppresses both this finding and the built-in O/P convention FIX."
      severity: "FAIL"
      detail: "Judgment-assisted from the ancestor CLAUDE.md chain. Flag ONLY when the exact declared rule is quotable VERBATIM from an ancestor (no inferred / generic conventions); the message carries the verbatim quote + the ancestor source path. The same declared rule + exception governs BOTH this check and the built-in non-ASCII (O) / hardcoded-path (P) FIX so the two can never contradict. Taxonomy S_ancestor_convention_violation; disposition FIX (mechanical correction), SERIOUS when the violation reveals a real-world problem the rule exists to prevent."
  taxonomy:
    - id: "A_misclassified_skill_ref"
      name: "Selected target is actually a skill reference"
      keywords: ["skill reference", "wrong auditor", "references folder", "misrouted"]
      detection_signal: "discover.py classified the path `skill_reference` (it sits inside a `*/skills/*/references/` folder)."
      default_remediation: "Skip the file in this audit and note it is covered by /md-audit skill (the SKILL.md that owns the references/ folder audits it transitively). No edit to the file -- a routing conclusion, not a finding against the doc."
      bucket: "SILENT"
    - id: "B_graduate_to_skill"
      name: "Matured content should graduate to a skill"
      keywords: ["graduate", "skill type", "technique", "discipline", "reference skill", "capability", "maturation"]
      detection_signal: "The doc's content has stabilized into a recognizable skill-typed shape (procedure / rule+counter / lookup table / tool wrapper) AND its natural trigger is TASK-shaped -- a verb the session performs (authoring an X, running an evaluator), needed wherever in the tree the activity happens. Only task-shaped stabilized content routes here; location-scoped knowledge routes to C, not B."
      default_remediation: "Propose graduating the doc into a skill of the matching type: name the type, the (task-shaped) trigger, and whether the doc content becomes structured SKILL.md content or moves into the skill's references/. Structural maturation move -> IMPROVE (one-line pitch; user opts in -- this is a new-skill authoring task, /md-authoring skill). Recommend this ONLY when the trigger is task-shaped."
      bucket: "IMPROVE"
    - id: "C_fold_into_claude_md"
      name: "Content homes to a directory CLAUDE.md (small tip, or location-scoped knowledge)"
      keywords: ["fold", "inline", "claude.md insight", "small tip", "guardrail", "location-shaped", "directory-scoped", "trigger shape"]
      detection_signal: "Either (a) the doc is small and load-bearing (a tip, a single fact, a one-line guardrail) over-promoted to a standalone doc; OR (b) the content's natural trigger is LOCATION-shaped -- knowledge scoped to a directory (a config subtree, source dirs, a package) needed when working under that directory. A directory CLAUDE.md auto-loads when files beneath it are touched, so it is the PREFERRED home for location-scoped knowledge (cohesion-principles placement_follows_trigger_shape) -- preferred over skill-graduation (B), not a lesser fallback."
      default_remediation: "Propose folding the content into (or referencing it from) the owning directory's CLAUDE.md -- for a small tip, inline it as an insight/convention and delete the standalone doc; for larger location-scoped knowledge, move it adjacent to that directory's CLAUDE.md or leave a pointer. This is the PREFERRED recommendation over B/D when the trigger is location-shaped. Structural move -> IMPROVE (one-line pitch; user confirms the destination CLAUDE.md)."
      bucket: "IMPROVE"
    - id: "D_move_into_existing_skill"
      name: "Content belongs in an existing skill's references/"
      keywords: ["move to skill", "existing skill", "references folder", "wrong home"]
      detection_signal: "An existing skill already owns the doc's topic; the content belongs inside that skill's references/ rather than as a parallel project doc."
      default_remediation: "Propose moving the doc into the owning skill's references/ folder and leaving a pointer (or nothing) behind. Structural move -> IMPROVE (one-line pitch; user confirms the target skill)."
      bucket: "IMPROVE"
    - id: "E_crp_split"
      name: "Doc serves multiple reading tasks (CRP split warranted)"
      keywords: ["crp split", "multiple reading tasks", "decomposition", "unitary"]
      detection_signal: "Over the size signal AND the lane judges sections serve genuinely different sub-triggers (setup + reference table + troubleshooting)."
      default_remediation: "Propose a CRP decomposition: which sections split out, and the destination scope for each (a skill, a CLAUDE.md, or a separate sibling doc). Offerable (IMPROVE) only with a NAMED extraction candidate; a bare over-threshold nudge with none is SILENT. One-line pitch; user confirms before splitting."
      bucket: "IMPROVE"
    - id: "F_chained_reference"
      name: "Cross-reference chain deeper than one hop"
      keywords: ["chained reference", "one hop", "transitive reference", "reference chain"]
      detection_signal: "The doc requires following a citation to a sibling doc that itself requires following a further citation (A -> B -> C reading path)."
      default_remediation: "Flatten to one hop: inline the second-hop content the reader needs, or restructure so each doc is understandable after one citation. Structural move -> IMPROVE (one-line pitch; user confirms)."
      bucket: "IMPROVE"
    - id: "G_claude_md_back_reference"
      name: "Doc cites CLAUDE.md section content as a dependency"
      keywords: ["back reference", "claude.md citation", "reverse edge", "load order"]
      detection_signal: "The body cites specific CLAUDE.md section content as required reading (reversing load order), beyond a permitted orientation mention."
      default_remediation: "Remove the back-citation, or inline the small fact the doc actually needs. Keep orientation mentions ('see the root CLAUDE.md') that do not depend on CLAUDE.md content. Structural move -> IMPROVE (one-line pitch; user confirms)."
      bucket: "IMPROVE"
    - id: "H_orphan"
      name: "Orphan -- nothing in the load graph points at the doc"
      keywords: ["orphan", "no inbound citation", "unreachable", "dangling", "discoverability"]
      detection_signal: "discover.py reports inbound_citations == 0 -- no CLAUDE.md / SKILL.md / sibling doc references the file by name."
      default_remediation: "Either add a one-line pointer from the owning CLAUDE.md (so the doc loads on demand for agents) or, if the doc is dead, retire it. IMPROVE by default (orphan-linking is a structural judgment; one-line pitch). An intentionally human-only doc -- a published design record, historical record, companion-source PDF, an agent-definition file with zero inbound citations -- is an accepted structural pattern -> SILENT (not surfaced)."
      bucket: "IMPROVE"
    - id: "I_duplicates_skill"
      name: "Doc duplicates content already owned by a skill"
      keywords: ["duplication", "ssot", "parallel reference", "skill content", "collapse to pointer"]
      detection_signal: "A skill covers the doc's topic and the doc restates (rather than points at) that skill's content."
      default_remediation: "Collapse the doc to a pointer ('for X, invoke /skill-name') so the skill's references/ stays the SSOT -- dedup under the summarize-and-reference rule (REMINDER PLUS REFERENCE, a dozen tokens or less, else reference-only). Loss-free-deletion guard first: fold any doc-local delta into the pointer/SSOT before deleting. That fold is a judgment no auto-apply pass can satisfy, so this is IMPROVE (opt-in one-line pitch), NOT FIX."
      bucket: "IMPROVE"
    - id: "J_size_signal"
      name: "Body over size threshold (CRP-evaluation prompt)"
      keywords: ["size signal", "line count", "token count", "crp evaluation"]
      detection_signal: "Mechanical INFO: effective lines > 500 or approx tokens > 3000."
      default_remediation: "Run the CRP test (do sections serve different reading tasks?). If yes AND a concrete extraction candidate can be named, escalate to E (IMPROVE). If no named candidate, SILENT -- a large single-task doc is correct and a bare over-threshold nudge is not offered."
      bucket: "IMPROVE"
    - id: "L_readme_stranded_fact"
      name: "Agent-relevant fact stranded in README"
      keywords: ["readme", "stranded fact", "unreachable command", "ssot", "derived brief"]
      detection_signal: "The doc is a README (role_hint == readme) and carries a command, convention, or schema that is not reachable through the CLAUDE.md / skill graph."
      default_remediation: "Move the fact's SSOT into the agent-facing graph (the owning CLAUDE.md or skill) and keep README as the derived brief; README may keep a human-facing copy once the graph owns the fact. Structural move -> IMPROVE (one-line pitch; user confirms the destination)."
      bucket: "IMPROVE"
    - id: "M_generated_missing_provenance"
      name: "Claimed-generated doc without a provenance signal"
      keywords: ["generated", "missing provenance", "no sidecar", "no marker", "unverifiable"]
      detection_signal: "The doc is claimed/presented as generated output but discover.py found neither a generation-record sidecar nor an in-file generation marker (generated == false)."
      default_remediation: "Add a machine-readable generation record -- a <name>.params.json sidecar recording exactly how to regenerate (the proven shape) -- or an in-file marker naming the generator/session. Needs the generator identity -> IMPROVE (one-line pitch; user confirms which)."
      bucket: "IMPROVE"
    - id: "N_broken_link_identified_target"
      name: "Broken outbound link with an identified target"
      keywords: ["broken link", "identified target", "moved file", "dead link", "mechanical fix"]
      detection_signal: "An outbound file-path / doc link does not resolve, AND the intended target is identifiable (the file moved to a findable path, or the link is a typo of an existing path). NOT a generator-owned path absent from the checkout (that is not broken -- see the generated_artifact rules)."
      default_remediation: "Re-point the link to the found target -- a correction against a verified fact. FIX. (If no target can be found, the link cites content that is gone: deleting the falsified reference is FIX; a structural re-home is IMPROVE.)"
      bucket: "FIX"
    - id: "O_non_ascii_lookalike"
      name: "Non-ASCII look-alike character in the body"
      keywords: ["non-ascii", "smart quote", "em dash", "unicode look-alike", "ascii only", "convention violation"]
      detection_signal: "The body contains a non-ASCII look-alike (smart quote, em dash, fullwidth char, non-breaking space) where the project convention is ASCII-only."
      default_remediation: "Replace the look-alike with its ASCII equivalent -- a documented-convention fix that loses nothing. FIX."
      bucket: "FIX"
    - id: "P_foreign_absolute_path"
      name: "Hardcoded foreign / machine-specific absolute path"
      keywords: ["absolute path", "foreign path", "machine-specific", "hardcoded path", "convention violation", "backslash path"]
      detection_signal: "The body hardcodes a machine-specific / foreign absolute path (a drive letter, a home dir, a per-machine root) where a project-relative path or a variable is the convention; or a backslash path where forward slashes are the convention."
      default_remediation: "Rewrite to the project-relative / conventional form (and backslash paths to forward slashes). Convention-violation fix -> FIX. (A validator artifact becomes FIX exactly here: the edit is also a genuine convention fix.)"
      bucket: "FIX"
    - id: "Q_line_drift"
      name: "Cited line number drifted from its symbol/anchor"
      keywords: ["line drift", "stale line number", "drop line number", "re-anchor", "convention violation"]
      detection_signal: "A claim cites a line number; the enclosing symbol/section it names resolves but is far from the cited number, and the author gave no recovery hint."
      default_remediation: "Drop the stale line number, keeping the symbol/section anchor. Mechanical convention-violation fix -> FIX."
      bucket: "FIX"
    - id: "R_stale_anchor"
      name: "Stale anchor re-pointable to a found mechanism"
      keywords: ["stale anchor", "broken anchor", "re-point", "found mechanism", "verified fact"]
      detection_signal: "A concrete anchor a claim makes (a symbol / heading / path the doc says should exist) is absent as cited but the current equivalent is findable. NOT a generator-owned absent path."
      default_remediation: "Re-point the anchor to the found current mechanism -- a correction against a verified fact. FIX. (When the anchor guards a protective rail with NO surviving mechanism, it is SERIOUS -- surface the unprotected invariant, do not auto-fix.)"
      bucket: "FIX"
    - id: "S_ancestor_convention_violation"
      name: "Doc violates a convention an ancestor CLAUDE.md explicitly declares (PD-11)"
      keywords: ["ancestor convention", "PD-11", "ascii-only", "no absolute paths", "declared convention", "verbatim rule"]
      detection_signal: "PD-11 hit: the doc violates a convention EXPLICITLY declared in an ancestor CLAUDE.md (loaded ambient), and the exact declared rule is quotable VERBATIM from that ancestor (no inferred / generic conventions). Fires only when ancestorClaudeMdPaths is supplied and non-empty; a doc with no ancestors emits nothing. Suppressed when an ancestor's explicitly declared scoped exception covers the exact instance."
      default_remediation: "FIX for a mechanical correction against the documented convention (replace a non-ASCII look-alike, relativize a hardcoded absolute path, apply the stated formatting rule); the message carries the verbatim ancestor rule quote + the ancestor source path. SERIOUS when the violation reveals a real-world problem the rule exists to prevent (e.g. a committed secret an ancestor forbids) -- surfaced at the top, never auto-fixed."
      bucket: "FIX"
    - id: "K_unclassified"
      name: "Unclassified / special case"
      keywords: ["unclassified", "special case", "escape hatch", "K bucket"]
      detection_signal: "Finding does not match any A-J / L / M / N-S detection signal after a deliberate attempt."
      default_remediation: "Surface to the user with the audit row that fired, attempted matches, and reasons none fit. User proposes strategy."
      bucket: "SPECIAL"
  procedures:
    - id: "audit_project_doc"
      name: "Audit project documents and dispatch remediations"
      keywords: ["audit", "project doc", "single-file audit", "compliance verdict", "dispatch"]
      goal: "For each target project document, run mechanical and judgment-based checks against the cohesion-principles project_reference_md role + maturation pipeline, classify findings into the taxonomy, assign each a disposition (FIX / SERIOUS / IMPROVE / SILENT; K -> SPECIAL) -- this audit is no longer blanket no-AUTO: the mechanical convention checks (N-S) are FIX -- and emit a per-file compliance verdict."
      preconditions:
        - "discover.py is reachable (enumerates candidate project docs + the mechanical orphan/size signals)."
        - "references/audit-criteria.md is loadable (the self-contained criteria doc; the upstream cohesion-principles is its derivation and is NOT loaded by the audit path)."
        - "The user is in a project directory so discoverability / orphan signals are meaningful."
      steps:
        - n: 1
          action: "Resolve the audit target set from $ARGUMENTS. Empty -> scan cwd and list. 'list' -> emit numbered list via discover.py and stop. A directory path -> scan it for project docs (discover.py --root). A file path -> audit it directly (discover.py --path). Integers -> map to paths from the last list. Strip any non-interactive token ('fast', '--fast', '--yes', '-y') first and set non_interactive accordingly (also set it if the user's prose expresses non-interactive intent). Strip the review token ('review', '--review') and set review=true (also set it if the user's prose expresses the intent, e.g. 'review my changes before I submit', 'audit the diff'); review is FALSE by default. Reject the combination review + non_interactive: 'propose instead of applying, but do not ask' resolves to doing nothing -- tell the user the two are mutually exclusive and stop. For each target capture (path, kind, role_hint, generated, generation_record, lines, approx_tokens, inbound_citations, cited_by) from discover.py --json, plus ancestorClaudeMdPaths (for the PD-11 ancestor-convention check): the FULL ancestor CLAUDE.md chain above the doc, enumerated deterministically -- starting from the doc's PARENT directory, walk up one directory at a time until (and including) the workspace root (the cwd when there is no enclosing project, otherwise the nearest ancestor containing a `.git` entry), and for each directory Glob/stat `<dir>/CLAUDE.md`; collect every one that exists into a list ordered NEAREST-ANCESTOR FIRST, EXCLUDING the doc itself. Empty when no ancestor CLAUDE.md exists. Drop targets whose kind is `skill_reference` or `other_claude_artifact` with an A_misclassified_skill_ref note (route them to the right auditor)."
          tool: "discover.py"
          input: "uv run python ${CLAUDE_PLUGIN_ROOT}/skills/project-doc-audit/scripts/discover.py [--root DIR | --path FILE ...] --json"
          expected: "Resolved per-doc records (path, kind, lines, approx_tokens, inbound_citations, cited_by) + non_interactive flag."
          on_failure: "If no project docs resolve, surface the scan root and stop."
        - n: 2
          action: "DETECT phase (before-Q&A). Choose execution mode by file count -- this threshold equalizes the Workflow tool's per-run overhead. REVIEW MODE OVERRIDE: when review is TRUE the threshold is 1, so ALWAYS use the Workflow path even for a single file. Review mode gates a submit/publish, so its verdict must not depend on whatever model the session happens to be running; only the lane pins model+effort and enforces the schema. Never run a review-mode detect inline. Named-role dispatch first: a `generated` target gets ONLY the PD-10 provenance check (all authored-doc criteria skipped); a `role_hint: readme` target gets the PD-9 readme-role criteria (maturation/orphan skipped, identity-grain overlap tolerated). ONE file (non-review): audit inline in the main loop (Read the doc; Read references/audit-criteria.md -- the single self-contained criteria doc; apply the project_reference_md criteria; use the mechanical signals discover.py already computed -- orphan from inbound_citations, size from lines/approx_tokens, role_hint/generated for named-role dispatch; if ancestorClaudeMdPaths is non-empty run the PD-11 ancestor-convention check inline exactly as the lane does -- read each ancestor CLAUDE.md, flag a subject violation ONLY when the declared rule can be quoted VERBATIM from the ancestor (no inferred/generic conventions), emit it as group Hygiene, taxonomy S_ancestor_convention_violation, FAIL, with the verbatim quote + ancestor source path in the message; exception awareness (applies to PD-11 AND to the built-in non-ASCII O / hardcoded-absolute-path P convention FIX in the classifier): when an ancestor EXPLICITLY declares a scoped exception that covers the specific instance -- right file scope AND right content kind, e.g. 'ASCII only, except developer names in the contributors section may contain non-ASCII characters' -- do NOT flag that instance under either check; demote it to PASS/INFO and cite the verbatim exception quote + ancestor source path in the message. The one declared rule + exception governs both checks so they never contradict (PD-11 silent while the built-in convention FIX still fires is exactly the bug this removes); no inferred or stretched exceptions, and when in doubt the check STILL fires; judge maturation / CRP / duplication; classify each finding into taxonomy + bucket). TWO OR MORE files (or ANY count in review mode): call the Workflow tool with scriptPath ${CLAUDE_PLUGIN_ROOT}/skills/project-doc-audit/workflow/detect.js and args = { files:[{path, kind, role_hint, generated, generation_record, lines, approx_tokens, inbound_citations, cited_by, ancestorClaudeMdPaths, preImagePath}], review:<review bool>, refs:{criteria, pluginRoot} }. The workflow fans one lane out per file and returns { perFile, totals, review }. In review mode YOU materialize each pre-image BEFORE calling the workflow (see the review-mode section) and pass its path; the workflow is VCS-agnostic and will not fetch anything itself. Detection only -- no file is edited in this phase."
          tool: "Workflow | inline"
          input: "detect.js args.refs: criteria=${CLAUDE_PLUGIN_ROOT}/skills/project-doc-audit/references/audit-criteria.md; pluginRoot=${CLAUDE_PLUGIN_ROOT}. (cohesion-principles is intentionally NOT passed -- lanes load only the self-contained criteria doc for cache efficiency.)"
          expected: "Structured per-file findings (group, severity, criterion, message, line, taxonomy, bucket, remediation) + per-file verdict."
          on_failure: "If the Workflow tool is not available in this environment (subagent contexts do not expose it), fall back to the ONE-file inline detect procedure run sequentially per file -- detection and remediation stay separate passes. EXCEPT in review mode, where this fallback does not apply: inline detection inherits the session model and forfeits the pin the gate depends on, so either stop and tell the user review mode needs a main-session run, or run inline and label the result advisory-and-unpinned rather than a passed gate. If a maturation/duplication judgment cannot be made cheaply (e.g. the candidate skill is ambiguous), mark it JUDGMENT/DISCUSS rather than FAIL."
        - n: 3
          action: "Render the per-file report (output_template), then the REPORT CONTRACT summary in three visible sections IN THIS ORDER, no hedging: (1) SERIOUS -- 'Found <N> serious issue(s) that require fixing' + a one-line summary each; never auto-fixed. (2) FIX -- normally the count auto-applied and landing in the reviewable remediation CL (the mechanical N-S convention fixes); in REVIEW MODE nothing is auto-applied, so render it as the count PROPOSED and awaiting the step-4 decision, never as applied. (3) IMPROVE -- 'Audit found <N> improvement opportunit(ies). Do you want to discuss them?' + one one-line pitch each. SILENT findings do NOT appear. Omit a section whose count is zero."
          expected: "Markdown report: per-file verdicts, then SERIOUS (summarized, top) / FIX (applied count) / IMPROVE (count + one-liners); SILENT omitted."
        - n: 4
          action: "Q&A GATE. If review is TRUE: NOTHING is auto-applied -- FIX is demoted from auto-apply to PROPOSED and goes to the user alongside IMPROVE/SPECIAL. Present proposals with AskUserQuestion offering accept-all / reject-all / custom instruction (use multiSelect when accept-some is the natural shape). Batch small related fixes into ONE question; split large or unrelated ones. SERIOUS is surfaced at the top as always and still never auto-fixed. Review-mode declines write NOTHING to `md-audit-declined:` -- that ledger is IMPROVE-scoped and per-file-permanent, whereas a review decline usually means 'not in this change', and once the change lands the finding is in the next pre-image and stops being attributable anyway. Offer an explicit 'never flag this again for this file' only if the user asks for it, and only then write the ledger. If review is FALSE and non_interactive is FALSE (default): SERIOUS findings are surfaced summarized at the top, never auto-fixed; for each IMPROVE and SPECIAL finding the user opted to discuss, ask for a decision (apply as-proposed / skip / a refined instruction). Surface a tight grouped set; do not dump a giant list. A declined IMPROVE is recorded in the doc's `md-audit-declined:` frontmatter so a re-audit does not re-pitch it. If non_interactive is TRUE: apply FIX findings, surface SERIOUS, and infer each IMPROVE/SPECIAL decision from the taxonomy's default_remediation plus the doc content -- record each inferred decision in the final summary. FIX findings need no decision; SILENT findings are never surfaced."
          expected: "SERIOUS summarized; a decision (explicit or inferred) attached to every IMPROVE/SPECIAL the user engaged; FIX applied."
        - n: 5
          action: "REMEDIATE phase (after-Q&A). Assemble per-file remediation lists from the decided findings (FIX=apply; IMPROVE/SPECIAL=per decision; SERIOUS never auto-applied; drop skips). ONE file: apply inline with Edit. TWO OR MORE files: call the Workflow tool with scriptPath ${CLAUDE_PLUGIN_ROOT}/skills/project-doc-audit/workflow/remediate.js and args = { perFile:[{path, remediations:[{criterion, taxonomy, bucket, line, instruction, decision}]}] }. One lane per file (disjoint files never conflict). NOTE: graduation (B), fold-into-CLAUDE.md (C), and move-into-skill (D) remediations are multi-file structural moves -- the lane applies the move it is instructed to make; new-skill authoring beyond a simple move should be handed to /md-authoring skill rather than performed blind."
          tool: "Workflow | inline"
          expected: "Edits applied; per-file applied/skipped/failed summary."
        - n: 6
          action: "Render the final summary: FIX applied per file, IMPROVE decisions, SERIOUS still-open (never auto-applied), any failures. Remind the user that re-running the audit should reproduce a clean (or reduced-FAIL) verdict -- detection and remediation are separate passes, so the re-run is the verification step. Scope the verification re-run to the files that were actually MODIFIED by remediation -- results for untouched files stand; re-auditing them wastes runs."
          expected: "Closing summary; user can re-run /md-audit project-doc on the modified files to verify FAILs cleared."
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

        ## Report (SERIOUS -> FIX -> IMPROVE; SILENT omitted, no hedging)

        ### SERIOUS -- Found <N> serious issue(s) that require fixing
        - <one-line summary per issue>   (never auto-fixed)

        ### FIX -- <N> applied (in the reviewable remediation CL)   [review mode: "<N> proposed" -- nothing is applied]
        - <criterion>: <what was corrected>

        ### IMPROVE -- Audit found <N> improvement opportunit(ies). Do you want to discuss them?
        - <criterion>: <one-line pitch>
      gotchas:
        - "discover.py classifies skill-attached references (*/skills/*/references/*.md) as `skill_reference` and CLAUDE.md/SKILL.md as `other_claude_artifact`. The audit only evaluates `project_doc`; a selected non-project-doc target produces a single A_misclassified_skill_ref INFO routing it to the right auditor."
        - "Orphan (H) is a JUDGMENT, never an auto-FAIL. A project doc can legitimately serve human readers outside the agent load graph (a published design record, a runbook a person opens). The audit surfaces the orphan; the user decides whether to add a pointer, retire it, or accept it."
        - "Maturation (B/C/D) findings are advisory -- a project doc doing useful work where it sits is COMPLIANT. Never FAIL a doc for not yet being a skill; a skill is NOT the default mature home. Route by TRIGGER SHAPE (cohesion-principles placement_follows_trigger_shape): task-shaped (a verb) -> skill (B); location-shaped (scoped to a directory) -> directory CLAUDE.md reference (C, the PREFERRED home for directory-scoped knowledge, since a CLAUDE.md auto-loads when files beneath it are touched); existing-skill-owns -> that skill's references/ (D). Do NOT pitch skill-graduation for location-scoped knowledge."
        - "Size is a SIGNAL (INFO/J), never a verdict. A large single-reading-task doc that passes CRP is correct; only escalate to E_crp_split when a CRP-passing decomposition genuinely exists."
        - "Cross-reference (broken skill-link) integrity is NOT this audit's job -- /md-audit references owns it. This audit checks doc-to-doc one-hop discipline and file-path resolution only."
        - "Named roles override the generic criteria: a generated artifact (sidecar / in-file marker) is audited for provenance ONLY -- never flag it for maturation, split, orphan, or duplication, and no in-doc exemption declaration is needed. A README is the human-facing derived brief -- never flag its identity-grain overlap with root CLAUDE.md as duplication, and never flag it as an orphan."
      anti_patterns:
        - id: "audit_then_self_remediate"
          name: "Audit and remediate in the same procedure pass"
          keywords: ["self-remediation", "single-pass", "idempotency"]
          why_it_seems_right: "Auditing one doc and applying remediations in the same pass seems efficient -- one tool call, fewer round trips."
          why_it_is_wrong: "Mixing detection and remediation breaks idempotency. The verdict and remediation are separate phases; conflating them prevents re-runs from producing the same findings."
          alternative: "Run the audit to completion. Render the verdict. Dispatch remediations as separate FIX (auto-applied) + IMPROVE (opt-in) work units after the Q&A gate, surface SERIOUS at the top. Re-run the audit to verify."
        - id: "flag_every_orphan_for_deletion"
          name: "Treat every orphan as dead weight"
          keywords: ["orphan", "delete", "human-only doc", "false positive"]
          why_it_seems_right: "An orphan has zero inbound citations, so nothing loads it -- looks like dead weight to remove."
          why_it_is_wrong: "Many project docs serve human readers who open them directly (design records, runbooks, onboarding). Zero agent-load-graph citations does not mean zero value. Deleting them destroys human-facing documentation."
          alternative: "Surface the orphan as a JUDGMENT. Offer three paths: add a CLAUDE.md pointer (make it agent-reachable), retire it (genuinely dead), or accept it (intentionally human-only). The user picks."
  # Disposition mapping (four-disposition model): structural lanes retained for
  # schema stability across audit members. auto = FIX categories (auto-applied;
  # land in the reviewable CL) -- this audit is NO LONGER blanket no-AUTO: the
  # mechanical convention checks N-S are FIX. I dedup is IMPROVE, not FIX: its
  # loss-free precondition (fold the doc's unique deltas into the skill BEFORE
  # removal) is a judgment no auto-apply pass can satisfy. discuss = SERIOUS
  # (never auto) + IMPROVE (opt-in) + SILENT-default (A routing) categories,
  # disposition noted per entry. special = K. The final per-finding disposition
  # is assigned instance-level by the detect.js classifier.
  remediations:
    auto:
      - category: "N_broken_link_identified_target"
        procedure: "[FIX] Re-point the broken outbound link to the found target (a verified fact). If no target exists, deleting the falsified reference is FIX; a structural re-home is IMPROVE. A generator-owned absent path is NOT broken -- annotate it 'auto-generated (present after doc-gen)' (FIX), or repoint/delete (IMPROVE)."
        agent_template: "Background agent receives the broken link + the identified target (or absence/generator proof); re-points, annotates, or deletes accordingly."
      - category: "O_non_ascii_lookalike"
        procedure: "[FIX] Replace the non-ASCII look-alike with its ASCII equivalent (documented ASCII-only convention). Byte-safe replacement."
        agent_template: "Background agent receives the offending character + line; replaces with the ASCII equivalent."
      - category: "P_foreign_absolute_path"
        procedure: "[FIX] Rewrite the hardcoded foreign/machine-specific absolute path to the project-relative/conventional form, and backslash paths to forward slashes."
        agent_template: "Background agent receives the path + line; rewrites to the conventional form."
      - category: "Q_line_drift"
        procedure: "[FIX] Drop the stale cited line number, keeping the symbol/section anchor."
        agent_template: "Background agent receives the claim line + drifted number + resolved anchor; strips the number."
      - category: "R_stale_anchor"
        procedure: "[FIX default] Re-point the stale anchor to the found current mechanism (a verified fact). SERIOUS instead when the anchor guards a protective rail with NO surviving mechanism -- surface the unprotected invariant, do not auto-fix."
        agent_template: "Background agent receives the stale anchor + found target (or no-surviving-mechanism proof); re-points, or escalates a rail to the SERIOUS surface."
      - category: "S_ancestor_convention_violation"
        procedure: "[FIX default] Apply the mechanical correction that satisfies the verbatim-quoted ancestor convention (replace the non-ASCII look-alike, relativize the absolute path, apply the stated formatting rule). SERIOUS instead when the violation reveals a real-world problem the rule exists to prevent (e.g. a committed secret) -- surface at the top, never auto-fix. Suppressed entirely when an ancestor's declared scoped exception covers the instance."
        agent_template: "Background agent receives the subject line + the verbatim ancestor rule quote + the ancestor source path; applies the convention-satisfying edit, or escalates a real-world-problem violation to the SERIOUS surface."
    discuss:
      - category: "A_misclassified_skill_ref"
        procedure: "[SILENT] Note the file is a skill reference and is audited via /md-audit skill (its owning SKILL.md). Skip it here; no edit. A routing conclusion, not surfaced as a finding against the doc (mention only in the operational skipped-files line)."
      - category: "B_graduate_to_skill"
        procedure: "[IMPROVE] Recommend ONLY when the trigger is TASK-shaped (a verb the session performs). Propose the skill type + task trigger and whether content becomes SKILL.md body or references/. Hand the actual authoring to /md-authoring skill. One-line pitch; user opts in. If the trigger is location-shaped, recommend C instead."
      - category: "C_fold_into_claude_md"
        procedure: "[IMPROVE] The PREFERRED recommendation for LOCATION-shaped knowledge (scoped to a directory) and for small load-bearing tips -- preferred over B/D, not a lesser fallback. Propose folding into (or referencing from) the owning directory's CLAUDE.md, which auto-loads when files beneath it are touched (zero session-wide context cost); delete the standalone doc for a small tip, or move larger location-scoped content adjacent. One-line pitch; user confirms the destination CLAUDE.md."
      - category: "D_move_into_existing_skill"
        procedure: "[IMPROVE] When an EXISTING skill already owns the topic (task-shaped, skill exists), propose moving the doc into that skill's references/ folder. One-line pitch; user confirms the target skill. For location-scoped knowledge with no owning skill, prefer C."
      - category: "E_crp_split"
        procedure: "[IMPROVE] Propose the decomposition: which sections split and the destination scope for each -- offerable only with a NAMED extraction candidate (else SILENT). One-line pitch; user confirms before splitting."
      - category: "F_chained_reference"
        procedure: "[IMPROVE] Flatten the reference chain to one hop (inline the second-hop content the reader needs). One-line pitch; user confirms."
      - category: "G_claude_md_back_reference"
        procedure: "[IMPROVE] Remove the back-citation or inline the small fact the doc needs; keep permitted orientation mentions. One-line pitch; user confirms."
      - category: "H_orphan"
        procedure: "[IMPROVE default] Offer: add a CLAUDE.md pointer (make agent-reachable) or retire the doc (dead). One-line pitch. An intentionally human-only / historical / companion-source doc is an accepted pattern -> SILENT (not surfaced)."
      - category: "I_duplicates_skill"
        procedure: "[IMPROVE] Collapse the doc to a pointer at the owning skill so the skill's references/ stays SSOT -- dedup under the summarize-and-reference rule (REMINDER PLUS REFERENCE). Loss-free-deletion guard: fold any doc-local delta into the pointer/SSOT before deleting. That fold is a judgment no auto-apply pass can satisfy, so this is opt-in (one-line pitch; user confirms the fold and pointer), NOT FIX."
      - category: "J_size_signal"
        procedure: "[IMPROVE default] Run the CRP test (do sections serve different reading tasks?). If yes AND a concrete extraction candidate can be named, escalate to E. If no named candidate, SILENT (the large single-task doc is correct)."
    special:
      procedure: "Surface the finding with the audit row that fired, attempted categories, and reasons none fit. User proposes strategy. Generalizable strategies become new taxonomy categories in references/audit-criteria.md."
  enforcement:
    gate_kind: "audit-finding"
    gating_rule: "FAIL findings (CRP chained reference, ADP back-reference into CLAUDE.md, CCP live duplication of skill content, README stranded agent facts (PD-9), unverifiable generation provenance (PD-10), broken file-path links (PD-H1), ancestor-declared convention violation (PD-11, when ancestorClaudeMdPaths is supplied)) gate compliance. JUDGMENT findings (maturation, orphan, CRP split candidacy) surface for review without gating; INFO findings are advisory only."
    appeal_process: "JUDGMENT findings are resolved by user confirmation (PASS once the user accepts the exception explicitly -- e.g. an intentionally human-only orphan). FAIL findings have no bypass; remediation is available within the taxonomy."
  gotchas:
    - "The subject is a corpus of project documents, but the audit procedure visits one file at a time. discover.py is the corpus enumerator + mechanical-signal source."
    - "Idempotency: criteria, taxonomy, and bucket assignments are fixed. Same input produces the same verdict; do not re-rank session-to-session."
    - "Known discover.py issues: the inbound-citation scan false-positives on dependency build trees (e.g. deps/ -- not in _SKIP_DIRS, so its files are read as citers/candidates) and counts stale working copies under .claude/worktrees/. Treat inbound counts sourced from those paths as suspect; the fix is extending _SKIP_DIRS in scripts/discover.py."
    - "Discoverability (orphan) detection scopes its citer scan to the whole project automatically -- discover.py decouples the inbound-citation root (--citer-root) from the candidate root (--root). Default citer-root = the candidates' project root by VCS marker (git/hg/svn/Perforce .p4config.txt), falling back to the launch cwd; so auditing a subdirectory like .claude/docs still sees citations from CLAUDE.md / skills elsewhere in the repo, even in a non-git (Perforce) project. Pass --citer-root explicitly for an unusual or multi-root layout."
    - "The inbound-citation scan also indexes INSTALLED PLUGIN-CACHE skills (SKILL.md + references from the harness plugin cache at <config>/plugins/cache, resolved via $CLAUDE_CONFIG_DIR or ~/.claude, filtered to enabled plugins when resolvable) as citation sources -- an installed skill is part of the load graph even though it lives outside the project tree. So a repo doc referenced only by an installed plugin skill is NOT reported as an orphan. Pass --skip-plugin-cache to scan the project tree only."
```

## Argument grammar

- `(none)` -- scan cwd for project docs and list them (does not audit; equivalent to `list`).
- `list` -- show a numbered list of project docs visible from the scan root; do not audit.
- `<dir>` -- audit every project doc under a directory (e.g. `/md-audit project-doc .claude/docs`).
- `<file>` -- audit a specific project doc.
- `<numbers>` -- audit docs by index from the most recent `list` output (e.g. `3 7 9`).
- `fast` / `--fast` / `--yes` / `-y` -- non-interactive: skip the Q&A round and infer every IMPROVE/SPECIAL decision; FIX applies by definition, SERIOUS is surfaced. Combine with any selector. Prose intent ("audit these and just apply everything, don't ask") sets the same flag.
- `review` / `--review` -- review mode: audit a CHANGE rather than a file. Findings the change did not cause are suppressed, nothing is auto-applied (FIX is proposed, not applied), and the verdict is `DIFF-CLEAN` rather than `COMPLIANT`. For gating a submit / publish / handback. Combine with any selector, e.g. `/md-audit project-doc Docs/Foo.md --review`. Prose intent ("review my changes before I submit", "audit the diff") sets the same flag. Mutually exclusive with `fast` -- see Review mode. Off by default.

Typical workflow: `/md-audit project-doc .claude/docs` to audit a whole doc home, or `/md-audit project-doc list` then `/md-audit project-doc 3 7` for specific files.

## Workflow orchestration

This skill runs in two phases split by an interactive Q&A gate, and uses the Workflow tool to fan the work out across files. **Invoking this skill authorizes the Workflow-tool calls described below** (the skill's instructions are the opt-in; do not re-prompt for permission to orchestrate).

```
resolve (main loop, via discover.py)
  -> DETECT  (before-Q&A)  : 1 file inline | 2+ files via workflow/detect.js   -> structured findings
                             (review mode: ALWAYS via workflow/detect.js, any file count)
  -> render report (main loop)
  -> Q&A GATE (main loop)  : interactive decisions | inferred when non-interactive
  -> REMEDIATE (after-Q&A) : 1 file inline | 2+ files via workflow/remediate.js -> edits applied
  -> final summary + "re-run to verify"
```

**Multi-file threshold (the overhead equalizer).** The Workflow tool has real per-run overhead. For a single file that overhead is not worth it, so a 1-file audit runs inline in the main loop. At 2+ files the parallel fan-out pays for itself, so detection (and, separately, remediation) go through the workflow scripts. **Fallback when the Workflow tool is not exposed** (subagent environments do not have it): run the 1-file inline procedure sequentially per file -- detection for all files first, then remediation, keeping the two as separate passes with the Q&A gate between them. **This fallback does NOT apply in review mode.** Inline detection inherits the session's model, which is exactly the property review mode's threshold-1 override exists to eliminate -- a gate whose strictness depends on which model the caller happened to be running is not a gate. If review mode is requested where the Workflow tool is unavailable, either stop and tell the user review mode needs a main-session run, or run inline and label the result explicitly as advisory-and-unpinned, never as a passed gate. Detection and remediation are **always separate passes** even in workflow mode -- the interactive Q&A sits between them, and a background workflow cannot ask the user anything.

**The two workflow scripts** (the detect script is hand-authored; the remediate script is generated from `scripts/gen_workflow_js.py` and drift-checked):

- `workflow/detect.js` -- before-Q&A. One lane per file: read the doc -> apply the project_reference_md criteria with the mechanical signals -> classify. Returns `{ perFile, totals }`. No edits.
- `workflow/remediate.js` -- after-Q&A. One lane per file (disjoint files, no conflicts): apply the decided edits. Returns `{ perFile, summary }`.

Both accept `args` as an object or JSON string. Pass absolute `refs` paths (they run from the session cwd, not the skill dir).

## Non-interactive mode

When the non-interactive flag is set (argument token or expressed intent), the Q&A gate does not prompt. Instead, infer each IMPROVE/SPECIAL decision from the taxonomy's `default_remediation` plus the doc content, apply them, and **list every inferred decision in the final summary** so the user can see and reverse them. FIX findings apply regardless; SERIOUS findings are surfaced summarized at the top and never auto-applied; SILENT findings are never surfaced. FAIL findings are still gated by the verdict; non-interactive only changes how the *decisions* are obtained. Interactive mode is the default.

## Review mode

Normal mode audits a FILE. Review mode audits a CHANGE: same criteria, same lanes, but findings the change did not cause are suppressed and nothing is auto-applied. It exists to gate a submit / publish / handback, where a report full of pre-existing findings would either bloat the change with unrelated remediations or train the author to skim past the gate.

Three behavioral differences, and nothing else:

1. **Attributability filter.** Each finding is marked `attributable` by the lane, then the caller drops the ones the change did not cause. **SERIOUS always survives regardless** -- a secret or a violated invariant is not the author's doing and is still the most important thing on the page.
2. **Nothing is auto-applied.** FIX is demoted to a proposal at the Q&A gate. Mutually exclusive with `fast`, which would mean "propose instead of applying, but do not ask" -- i.e. nothing. Reject that combination rather than guessing.
3. **Verdict is `DIFF-CLEAN`, not `COMPLIANT`.** A weaker and more honest claim: *this change introduced no failure*, not *this file is clean*. A DIFF-CLEAN doc may still carry a surviving SERIOUS.

Also: the multi-file threshold drops to 1 (always use the Workflow path), because a submit gate must not inherit whatever model the session happens to be running.

**You materialize the pre-images; the workflow never does.** This plugin is VCS-agnostic and must stay that way -- do not teach `detect.js` about Perforce or git. Before calling the workflow, write each doc's pre-change content to a temp path and pass it as `preImagePath`:

- Perforce: `p4 print -q -o <tmp> //depot/path/DOC#have`
- git: `git show <base>:<path> > <tmp>`, base = `merge-base(HEAD, origin/main)`, with the diff spanning `base..worktree` so committed-but-unpushed work is INSIDE the change under review rather than part of its baseline.

Infer which from the local repo. **Adds have no pre-image** -- pass `preImagePath: null` and every finding is attributable, which is correct (the whole doc is new). `p4 diff` emits nothing for an add, so detect adds via `p4 opened` rather than concluding the diff is unavailable; `p4 print //new/path#have` fails for a `move/add`, so resolve the pre-image through the move source.

Review mode also uses the PD-11 ancestor-convention input: pass `ancestorClaudeMdPaths` (the nearest-ancestor-first CLAUDE.md chain above the doc) so a change that violates an ancestor-declared convention is caught. When the code-review subject-lens caller drives this (git-kit / p4-kit), it derives that chain from the claimed file's `claude_mds`.

**If you cannot obtain a pre-image, do not silently fall back to a whole-file audit.** Say the pre-image was unavailable and label the output as unfiltered, so nobody mistakes a normal audit for a change-scoped gate.

Attributability is judgment, not arithmetic -- it rests on re-detection, so a pre-existing finding the pre-image check happens to miss can resurface as attributable. Generous structural matching mitigates this; nothing eliminates it.

## Decision rules

- Any FAIL finding -> file is NON-COMPLIANT.
- Only PASS / INFO / JUDGMENT findings -> file is COMPLIANT.
- INFO and JUDGMENT findings are advisory; they do not escalate to FAIL on subsequent runs even if unaddressed.
- **Review mode:** any *attributable* FAIL -> NON-COMPLIANT; otherwise DIFF-CLEAN. Non-attributable FAILs do not gate -- they predate the change -- but a non-attributable SERIOUS is still reported above the verdict.

## Cross-references

- Canonical placement framework: `cohesion-principles` (in skills-kit). The criteria in this skill's `references/audit-criteria.md` derive directly from that skill's `project_reference_md` role + `skill_maturation_pipeline`; when the two diverge, the canonical framework wins.
- Sibling audit skills: `skill-audit` (via `/md-audit skill`) for SKILL.md files; `claude-md-audit` (via `/md-audit claude-md`) for CLAUDE.md files; `references-audit` (via `/md-audit references`) for broken skill cross-references across markdown.
- Authoring counterpart: when a finding recommends graduation (B), absorption (C/D), or a split (E), the actual authoring is handed to `/md-authoring` (skill or claude-md) -- this audit detects and routes; it does not author new skills blind.
- Framework registry: the `project_doc_audit` audit-kind in `md-audit/references/audit-framework.yaml` binds this skill's criteria to the `plain_md` primitive over the `directory` / `project` compositions.
