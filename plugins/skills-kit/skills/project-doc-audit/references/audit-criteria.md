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

Project references are the **escape-hatch / nursery**, not the default home for reference content. The default home is a skill's `references/` folder. The placement criteria check whether the content has matured past a standalone doc.

### PD-1. Genuine project doc, not a mis-placed skill reference

**Rule:** the target is classified `project_doc` by discover.py. A file inside a `*/skills/*/references/` folder is a skill reference, audited via `/md-audit skill`; a `CLAUDE.md` / `SKILL.md` is audited by its own member.

**Test:** read discover.py `kind`. If `skill_reference` or `other_claude_artifact`, emit one routing finding (taxonomy A) and skip the rest.

**Severity:** INFO (a routing note, not a defect in the file).

### PD-2. Content earns its place as a project doc (maturation)

**Rule:** the doc's content should not have matured past the project-doc stage. Three maturation signals, in order of leverage:

1. **Graduate to a skill** (taxonomy B) -- content has stabilized into a skill-typed shape: a procedure (technique-skill), a rule + counter (discipline-skill), a lookup table (reference-skill), or a wrapper around an external tool/API (capability-skill), with a clear trigger. This is the highest-leverage destination -- a discoverable trigger, an audit surface, a typed contract.
2. **Fold into a CLAUDE.md** (taxonomy C) -- content is small and load-bearing (a tip, a single fact, a one-line guardrail). Stage 1 of the maturation pipeline; it was over-promoted to a standalone doc.
3. **Move into an existing skill** (taxonomy D) -- an existing skill already owns the topic; the content belongs in that skill's `references/`, not as a parallel project doc.

**Why:** `prefer_skill_reference` + `skill_maturation_pipeline`. Structured procedural / rule / lookup / wrapper content has more leverage as a skill; leaving it as an unstructured project reference forfeits the trigger, the audit surface, and the typed contract.

**Test:** read the doc; ask which (if any) maturation signal fires. If the content is still genuinely unstructured / emerging, none fires and the doc is correctly a project doc (PASS).

**Severity:** JUDGMENT (INFO-level). A project doc doing useful work where it sits is COMPLIANT -- graduation is an opportunity, never a defect.

## CRP findings (single reading task)

CRP says: every reader who lands on the doc should need all of it.

### PD-3. Unitary reading task

**Rule:** all sections of the doc fire on the same sub-trigger. A doc bundling content that fires on different sub-triggers (a setup guide + an API reference table + a troubleshooting log) serves multiple reading tasks and should split, each part landing at the scope whose readers all need it.

**Why CRP:** a reader who needs the setup guide does not need the troubleshooting log; bundling them taxes every reader with content they did not come for.

**Test:** enumerate the doc's sections; for each, judge whether it loads in the same situation as the others. Size (discover.py `lines` / `approx_tokens`) is a SIGNAL that prompts this evaluation, never a verdict.

**Severity:** JUDGMENT. Split (taxonomy E) only when a CRP-passing decomposition genuinely exists.

### PD-7 (CRP size signal). Body over threshold

**Rule:** effective lines > 500 or approx tokens > 3000 prompts a CRP evaluation.

**Test:** mechanical, from discover.py. Triggers the PD-3 unitary-reading-task check.

**Severity:** INFO (taxonomy J). Size alone is never FAIL; a large single-task doc that passes CRP is correct.

## ADP findings (load-graph direction + discoverability)

ADP says: file references run downward in load order, and the doc must be reachable in the graph.

### PD-4. Discoverability (not an orphan)

**Rule:** a project doc is loaded on demand when a `CLAUDE.md` / `SKILL.md` / sibling doc cites it by name. A doc with zero inbound citations is an orphan -- nothing in the agent load graph points to it, so it never loads for an agent.

**Why ADP:** an orphan is an unreachable node. For agent-facing docs this is a dead edge.

**Test:** discover.py `inbound_citations == 0`.

**Severity:** JUDGMENT (taxonomy H). NEVER auto-FAIL: a project doc can legitimately serve human readers who open it directly (a published design record, a runbook, onboarding material). The audit surfaces the orphan; the user decides among: add a CLAUDE.md pointer (make it agent-reachable), retire it (dead), or accept it (intentionally human-only -> PASS).

### PD-5. One-hop-deep cross-references

**Rule:** a project doc may cite a sibling project doc or a SKILL.md by name (informational pointer), but a cross-reference *chain* (A -> B -> C as a required reading path) is prohibited -- readers tend to stop at the second hop.

**Why ADP:** deeper chains leave readers with partial content.

**Test:** scan outbound doc-to-doc citations; verify the cited doc is understandable without requiring a further citation.

**Severity:** FAIL (taxonomy F) on chains deeper than one hop.

### PD-6. No back-reference into CLAUDE.md sections

**Rule:** a project doc is loaded AFTER the CLAUDE.md that cites it. It must not cite CLAUDE.md *section content* as a dependency (that reverses load order). It may name the CLAUDE.md as an orientation surface.

**Why ADP:** citing back into an upstream surface reverses the load direction; the reference runs after CLAUDE.md and cannot depend on having re-read it.

**Test:** scan for `CLAUDE.md` mentions that cite specific section content as required reading. Pure orientation mentions ("see the root CLAUDE.md for project setup") are permitted.

**Severity:** FAIL (taxonomy G) on dependency back-citations.

## CCP findings (no duplication of skill content)

CCP/SSOT says: when a skill owns a topic, its `references/` is the single source of truth.

### PD-8. No duplication of skill content

**Rule:** a project doc must not restate content that already lives in a skill. When a skill exists for the doc's topic, the doc should collapse to a pointer (`for X, invoke /skill-name`).

**Why CCP/SSOT:** two copies drift independently; the project doc and the skill diverge.

**Test:** check whether a skill covers the doc's topic and whether the doc restates (rather than points at) that skill's content.

**Severity:** FAIL (taxonomy I) on live parallel duplication. INFO when the project ref predates the skill and graduation is in progress.

## Hygiene findings (universal)

### PD-H1. Outbound file links resolve

Every path-like reference in the doc (`see docs/X.md`, relative links) resolves to a file that exists.

**Severity:** FAIL on broken file-path references.

### PD-H2. Cross-reference (skill-link) integrity is out of scope

Broken `/skill-name` and `skill: "..."` references are NOT checked here -- `/md-audit references` owns skill-link integrity. This audit checks doc-to-doc one-hop discipline (PD-5) and file-path resolution (PD-H1) only. Do not duplicate the references-audit scan.

## Output format

### Per-file report

```
## <file path> (<kind>, <lines>L, <inbound> inbound)

### Placement (maturation / home)
[PASS]     PD-2: content is genuinely emerging; correctly a project doc
[JUDGMENT] PD-2: content is a stabilized procedure -> graduate to a technique-skill (taxonomy B)

### CRP (single reading task)
[JUDGMENT] PD-3: sections "Setup" and "Troubleshooting" fire on different sub-triggers -> split candidate (E)

### ADP (load-graph direction + discoverability)
[JUDGMENT] PD-4: 0 inbound citations -- orphan; add a CLAUDE.md pointer, retire, or accept as human-only (H)
[FAIL]     PD-6: line 41 cites "the Insights section of the root CLAUDE.md" as required reading (G)

### CCP (no duplication of skill content)
[PASS]     PD-8: no skill owns this topic

### Hygiene (universal)
[FAIL]     PD-H1: line 88 link to "docs/old-plan.md" does not resolve

### Compliance
<P> PASS | <F> FAIL | <I> INFO | <J> JUDGMENT
COMPLIANT | NON-COMPLIANT
```

### Decision rules

- Any FAIL finding (PD-5 chain, PD-6 back-reference, PD-8 live duplication, PD-H1 broken link) -> file is NON-COMPLIANT.
- Only PASS / INFO / JUDGMENT findings -> file is COMPLIANT.
- JUDGMENT findings (PD-2 maturation, PD-3 split candidacy, PD-4 orphan) and INFO findings are advisory; they do not escalate to FAIL on subsequent runs even if unaddressed.
