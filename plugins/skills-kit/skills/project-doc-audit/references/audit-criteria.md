# Project Document Audit Criteria

The full criteria for the project-doc-audit member (reached via `/md-audit project-doc`). Organized by cohesion principle (Placement / CRP / ADP / CCP) plus universal hygiene. Each criterion has a testable rule and a severity (FAIL / JUDGMENT / INFO / PASS); decision rules at the bottom.

The placement principles these criteria derive from live in the `cohesion-principles` skill (`plugins/skills-kit/skills/cohesion-principles/SKILL.md`), specifically the `project_reference_md` per-artifact role and the `skill_maturation_pipeline`. When the two diverge, cohesion-principles wins; this file gets updated to match.

## What is a project document

A *project document* is a standalone reference doc that is:

- **NOT** a `SKILL.md` (audited by `/md-audit skill`),
- **NOT** a `CLAUDE.md` / `CLAUDE.local.md` (audited by `/md-audit claude-md`),
- **NOT** inside a `*/skills/*/references/` folder (those are skill references, audited transitively via their owning SKILL.md).

It lives at a project-level path -- `Docs/*.md`, `Docs/**/*.md.html` (Markdeep), `.claude/docs/*.md`, `<subsystem>/docs/*.md` -- or is a README / design note / hand-off plan (`plain_md`). `discover.py` classifies each candidate `project_doc` / `skill_reference` / `other_claude_artifact` and computes the mechanical signals the criteria use (effective lines, approx tokens, inbound citation count).

This audit enforces the **generic** cohesion framework. It does NOT impose a specific project's documentation-home policy (e.g. "design docs go in Docs/, agent quick-refs go in .claude/docs/") -- that belongs in the project's own CLAUDE.md, layered on top of these criteria.

## Placement findings (maturation / home)

Project references are the **escape-hatch / nursery** for still-emerging content, not a permanent home. When content stabilizes it moves to its **trigger-appropriate mature home** -- and a skill is **NOT** the default home for all reference content. The placement question is: what is the natural TRIGGER for needing this knowledge (cohesion-principles `placement_follows_trigger_shape`)? A **task-shaped** trigger (a verb the session performs) homes to a skill; a **location-shaped** trigger (knowledge scoped to a directory) homes to that directory's CLAUDE.md -- the preferred home for directory-scoped knowledge, since a CLAUDE.md auto-loads when any file beneath it is touched, at zero session-wide context cost. The placement criteria check whether the content has stabilized past the nursery stage and, if so, route it by trigger shape.

### PD-1. Genuine project doc, not a mis-placed skill reference

**Rule:** the target is classified `project_doc` by discover.py. A file inside a `*/skills/*/references/` folder is a skill reference, audited via `/md-audit skill`; a `CLAUDE.md` / `SKILL.md` is audited by its own member.

**Test:** read discover.py `kind`. If `skill_reference` or `other_claude_artifact`, emit one routing finding (taxonomy A_misclassified_skill_ref) and skip the rest.

**Severity:** INFO (a routing note, not a defect in the file).

### PD-2. Content earns its place as a project doc (maturation)

**Rule:** if the doc's content has stabilized past the nursery stage, route it to its trigger-appropriate mature home. Match the home to the natural TRIGGER SHAPE (not "always a skill"):

1. **Graduate to a skill** (taxonomy B_graduate_to_skill) -- ONLY when the trigger is **task-shaped**: a VERB the session performs (authoring an X, running an evaluator, classifying a CL), needed wherever in the tree the activity happens, and the content fits a skill type (procedure -> technique; rule+counter -> discipline; lookup table -> reference; tool/API wrapper -> capability). A skill is loaded by matching the session-wide skill list -- the right home for activity-triggered knowledge.
2. **Fold into / reference from a directory CLAUDE.md** (taxonomy C_fold_into_claude_md) -- the **preferred** home for **location-shaped** knowledge (scoped to a directory: a config subtree, source dirs, a package) and for small load-bearing tips. A directory CLAUDE.md auto-loads when any file beneath it is touched -- the exactly-right trigger for directory-scoped knowledge, at zero session-wide context cost. This is preferred over B/D when the trigger is location-shaped, **not** a lesser fallback.
3. **Move into an existing skill** (taxonomy D_move_into_existing_skill) -- an existing (task-shaped) skill already owns the topic; the content belongs in that skill's `references/`, not as a parallel project doc.

Knowledge with BOTH shapes: prefer the **location home** (C_fold_into_claude_md); a skill may point at it via summarize-and-reference.

**Why:** `placement_follows_trigger_shape` + `skill_maturation_pipeline`. A skill spends session-wide context budget and fires by description match; a directory CLAUDE.md spends nothing until the session works under the directory, then loads precisely. Matching the home to the trigger shape puts directory-scoped knowledge where it costs nothing and loads exactly, and reserves skills for activity-triggered knowledge. Skills-kit is NOT of the view that all mature documentation graduates to a skill.

**Test:** read the doc; ask "what is the natural trigger for needing this?" A verb -> task-shaped (skill, B). Working under a directory -> location-shaped (directory CLAUDE.md, C). Do NOT recommend skill-graduation for location-scoped knowledge. If the content is still genuinely unstructured / emerging, no signal fires and the doc is correctly a project doc (PASS).

**Severity:** JUDGMENT (INFO-level). A project doc doing useful work where it sits is COMPLIANT -- reaching the mature home is an opportunity, never a defect.

## CRP findings (single reading task)

CRP says: every reader who lands on the doc should need all of it.

### PD-3. Unitary reading task

**Rule:** all sections of the doc fire on the same sub-trigger. A doc bundling content that fires on different sub-triggers (a setup guide + an API reference table + a troubleshooting log) serves multiple reading tasks and should split, each part landing at the scope whose readers all need it.

**Why CRP:** a reader who needs the setup guide does not need the troubleshooting log; bundling them taxes every reader with content they did not come for.

**Test:** enumerate the doc's sections; for each, judge whether it loads in the same situation as the others. Size (discover.py `lines` / `approx_tokens`) is a SIGNAL that prompts this evaluation, never a verdict.

**Severity:** JUDGMENT. Split (taxonomy E_crp_split) only when a CRP-passing decomposition genuinely exists.

### PD-7 (CRP size signal). Body over threshold

**Rule:** effective lines > 500 or approx tokens > 3000 prompts a CRP evaluation.

**Test:** mechanical, from discover.py. Triggers the PD-3 unitary-reading-task check.

**Severity:** INFO (taxonomy J_size_signal). Size alone is never FAIL; a large single-task doc that passes CRP is correct.

## ADP findings (load-graph direction + discoverability)

ADP says: file references run downward in load order, and the doc must be reachable in the graph.

### PD-4. Discoverability (not an orphan)

**Rule:** a project doc is loaded on demand when a `CLAUDE.md` / `SKILL.md` / sibling doc cites it by name. A doc with zero inbound citations is an orphan -- nothing in the agent load graph points to it, so it never loads for an agent.

**Why ADP:** an orphan is an unreachable node. For agent-facing docs this is a dead edge.

**Test:** discover.py `inbound_citations == 0`.

**Severity:** JUDGMENT (taxonomy H_orphan). NEVER auto-FAIL: a project doc can legitimately serve human readers who open it directly (a published design record, a runbook, onboarding material). The audit surfaces the orphan; the user decides among: add a CLAUDE.md pointer (make it agent-reachable), retire it (dead), or accept it (intentionally human-only -> PASS).

### PD-5. One-hop-deep cross-references

**Rule:** a project doc may cite a sibling project doc or a SKILL.md by name (informational pointer), but a cross-reference *chain* (A -> B -> C as a required reading path) is prohibited -- readers tend to stop at the second hop.

**Why ADP:** deeper chains leave readers with partial content.

**Test:** scan outbound doc-to-doc citations; verify the cited doc is understandable without requiring a further citation.

**Severity:** FAIL (taxonomy F_chained_reference) on chains deeper than one hop.

### PD-6. No back-reference into CLAUDE.md sections

**Rule:** a project doc is loaded AFTER the CLAUDE.md that cites it. It must not cite CLAUDE.md *section content* as a dependency (that reverses load order). It may name the CLAUDE.md as an orientation surface.

**Why ADP:** citing back into an upstream surface reverses the load direction; the reference runs after CLAUDE.md and cannot depend on having re-read it.

**Test:** scan for `CLAUDE.md` mentions that cite specific section content as required reading. Pure orientation mentions ("see the root CLAUDE.md for project setup") are permitted.

**Severity:** FAIL (taxonomy G_claude_md_back_reference) on dependency back-citations.

## CCP findings (no duplication of skill content)

CCP/SSOT says: when a skill owns a topic, its `references/` is the single source of truth.

### PD-8. No duplication of skill content

**Rule:** a project doc must not restate content that already lives in a skill. When a skill exists for the doc's topic, the doc should collapse to a pointer (`for X, invoke /skill-name`).

**Why CCP/SSOT:** two copies drift independently; the project doc and the skill diverge.

**Test:** check whether a skill covers the doc's topic and whether the doc restates (rather than points at) that skill's content.

**Severity:** FAIL (taxonomy I_duplicates_skill) on live parallel duplication. INFO when the project ref predates the skill and graduation is in progress. (FAIL is the compliance severity; the remediation disposition is IMPROVE -- opt-in -- because the loss-free precondition below is a judgment no auto-apply pass can satisfy.)

## Named-role findings (README, generated artifacts)

Two per-artifact roles from cohesion-principles override the generic project-doc criteria. `discover.py` computes both signals mechanically (`role_hint`, `generated` / `generation_record`).

### PD-9. README is the derived human brief (readme_md role)

**Rule:** a README (`role_hint == "readme"`) is judged under the cohesion-principles `readme_md` role, NOT the generic project-doc criteria. Readers are humans and web crawlers; the agent-facing copy (CLAUDE.md / skill graph) is the SSOT and README is the derived brief. Two consequences:

1. **Tolerated overlap:** identity/architecture overlap with the root CLAUDE.md at the identity-sentence grain is NOT a duplication finding (skip PD-8 for that overlap). INFO when the overlap grows past the brief grain into synchronized multi-section restatement.
2. **No stranded agent facts (FAIL):** every command, convention, or schema present in README must also be reachable through the CLAUDE.md / skill graph. Agents never load README; a README-only fact is invisible to every session.

**Also skipped for READMEs:** PD-2 maturation (a README never graduates to a skill) and PD-4 orphan (a README is intentionally human-facing; zero inbound citations is its normal state).

**Test:** for each command block / convention / schema in the README, verify the fact (or its SSOT) is reachable from a CLAUDE.md or skill surface.

**Severity:** FAIL (taxonomy L_readme_stranded_fact) on stranded agent-relevant facts; INFO on overlap past the identity-sentence grain.

### PD-10. Generated artifacts: provenance only (generated_artifact role)

**Rule:** a committed generated output (`generated == true` -- identified by a generation-record sidecar like `<name>.params.json`, or an in-file generation marker in the first ~20 lines) is exempt from ALL other criteria -- the authored-doc criteria (PD-2 maturation, PD-3 CRP split, PD-4 orphan, PD-8 duplication, size signals) AND the hygiene criteria including PD-H1 link resolution (a broken-looking string in generated output is the generator's business; regenerating fixes it). One check applies: the generator or session provenance is named -- which the identifying signal itself establishes.

**Minimum marker content:** an in-file generation marker qualifies as provenance ONLY if it names the generator (tool, script, model, or session) or states the regeneration command/recipe. A bare assertion of generated-ness (e.g. "this document is generated analysis") does NOT qualify -- it asserts generated-ness without establishing provenance. A generation-record sidecar always qualifies (it is machine-readable by construction).

**The FAIL case:** a doc that *claims* to be generated (title/header says "generated", user asserts it) but carries neither a sidecar nor a qualifying in-file marker -- unverifiable provenance (taxonomy M_generated_missing_provenance). This includes a bare generated-assertion with no named generator. Remediation: add a machine-readable generation record (the sidecar pattern -- a `<name>.params.json` recording exactly how to regenerate -- is the proven shape) or an explicit in-file marker naming the generator.

**Test:** mechanical, from discover.py `generated` / `generation_record`. When `generation_record` is marker-type (in-file, not sidecar), the lane additionally verifies the marker meets the minimum-content bar above -- a marker signal alone does not end the check.

**Severity:** PASS with provenance (all other criteria skipped); FAIL (taxonomy M_generated_missing_provenance) on claimed-generated without a signal, including a signal that fails the minimum-content bar.

## Hygiene findings (universal)

### PD-H1. Outbound file links resolve

Every path-like reference in the doc (`see docs/X.md`, relative links) resolves to a file that exists.

**Severity:** FAIL on broken file-path references.

### PD-H2. Cross-reference (skill-link) integrity is out of scope

Broken `/skill-name` and `skill: "..."` references are NOT checked here -- `/md-audit references` owns skill-link integrity. This audit checks doc-to-doc one-hop discipline (PD-5) and file-path resolution (PD-H1) only. Do not duplicate the references-audit scan.

### PD-11. Obeys ancestor-declared conventions

A convention EXPLICITLY declared in an ancestor CLAUDE.md (ASCII-only mandates, "no absolute paths in shared files", stated formatting/structure rules) loads ambient in any session that touches this doc, so it binds the doc too. Flag a subject violation ONLY when the exact declared rule can be quoted VERBATIM from an ancestor (mirror the code-review reviewer_a rule-extraction posture -- no inferred conventions, no generic best-practice, no "spirit of" a rule). The finding (group Hygiene, taxonomy `S_ancestor_convention_violation`, severity FAIL) carries the verbatim ancestor rule quote + the source path of the ancestor that declared it. Disposition FIX for a mechanical correction; SERIOUS when the violation reveals a real-world problem the rule exists to prevent (e.g. a committed secret an ancestor forbids).

**Scope:** fires only when ancestor CLAUDE.md files are supplied to the audit (the `ancestorClaudeMdPaths` argument, nearest-ancestor first). A doc with no ancestor CLAUDE.md, or a run that supplies no ancestor paths, emits no PD-11 findings.

**Ancestor-declared exceptions suppress the built-in universal conventions.** The classifier also carries hardcoded universal-convention checks (a non-ASCII look-alike O or a hardcoded absolute path P is a convention-violation FIX unconditionally). Those are made **exception-aware** by the same ancestor CLAUDE.md declarations PD-11 reads: when an ancestor **explicitly declares a scoped exception** that covers the specific instance -- the right file scope AND the right content kind, e.g. *"ASCII only, except developer names in the contributors section may contain non-ASCII characters"* -- the built-in check does NOT emit the FIX; it demotes to PASS/INFO citing the verbatim exception quote + ancestor source path. The exception must be written down and actually cover the instance (same verbatim posture as PD-11; no inferred or stretched exceptions, and when in doubt the built-in check still fires). Precedence is deliberate: PD-11 and the built-in check read the *same* declared rule + exception, so they must yield one consistent outcome -- an exception that silences PD-11 silences the built-in FIX too, and vice versa. This is the contradiction the exception-awareness removes (PD-11 silent while the built-in check fires on the same instance). When no ancestor paths are supplied, or no exception is declared, the built-in checks behave exactly as before.

## Output format

### Per-file report

```
## <file path> (<kind>, <lines>L, <inbound> inbound)

### Placement (maturation / home)
[PASS]     PD-2: content is genuinely emerging; correctly a project doc
[JUDGMENT] PD-2: content is a stabilized procedure -> graduate to a technique-skill (taxonomy B_graduate_to_skill)

### CRP (single reading task)
[JUDGMENT] PD-3: sections "Setup" and "Troubleshooting" fire on different sub-triggers -> split candidate (E_crp_split)

### ADP (load-graph direction + discoverability)
[JUDGMENT] PD-4: 0 inbound citations -- orphan; add a CLAUDE.md pointer, retire, or accept as human-only (H_orphan)
[FAIL]     PD-6: line 41 cites "the Insights section of the root CLAUDE.md" as required reading (G_claude_md_back_reference)

### CCP (no duplication of skill content)
[PASS]     PD-8: no skill owns this topic

### Hygiene (universal)
[FAIL]     PD-H1: line 88 link to "docs/old-plan.md" does not resolve

### Compliance
<P> PASS | <F> FAIL | <I> INFO | <J> JUDGMENT
COMPLIANT | NON-COMPLIANT
```

### Decision rules

- Any FAIL finding (PD-5 chain, PD-6 back-reference, PD-8 live duplication, PD-9 stranded agent facts in README, PD-10 unverifiable generation provenance, PD-H1 broken link, PD-11 ancestor-convention violation) -> file is NON-COMPLIANT.
- Only PASS / INFO / JUDGMENT findings -> file is COMPLIANT.
- JUDGMENT findings (PD-2 maturation, PD-3 split candidacy, PD-4 orphan) and INFO findings are advisory; they do not escalate to FAIL on subsequent runs even if unaddressed.
