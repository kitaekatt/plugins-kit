# Example audit: writing-skills against discipline-skill contract

A worked example of running an audit against the framework. The subject is
the obra/superpowers `writing-skills` SKILL.md
(https://github.com/obra/superpowers/blob/main/skills/writing-skills/SKILL.md),
audited against the discipline-skill contract in `framework.md`.

This document is intentionally preserved as an artifact of an audit, not
rewritten as didactic prose. The point of an example audit is to show how
the contract checklist plays out on a real skill -- including the friction
the framework surfaces about itself.

---

## Type assignment

The skill is implicitly a discipline-skill (it enforces rules about how
skills should be authored). Audited against that claim.

The skill's stated intent: enforce discipline around skill authoring (run
baseline tests, close loopholes, write description as triggers not
workflow, etc.). Discipline-skill is the closest match for the *intent*,
but the skill's content also includes substantial reference material
(SKILL.md structure, frontmatter rules, locations) and pattern material
(the red/green/refactor mental model for skill authoring). It's a mixed-type skill in practice.

---

## Contract checklist

### Required blocks

| Item | Status | Notes |
|---|---|---|
| SKILL.md file with frontmatter and trigger | pass | frontmatter present, description starts with "Use when..." |
| >=1 rule + counter pair | fail | The skill teaches *how to write* rule+counter pairs and shows a template, but contains zero rule+counter pairs for its own rules. The shadowing rule, the description rule, the inline-content rule -- none have explicit rationalization counters. |
| Rationalization counter table | fail | A template appears demonstrating the pattern, but no actual table exists for this skill's own rules. |

### Required patterns

| Pattern | Status | Notes |
|---|---|---|
| activation metadata | pass | "Use when creating new skills, editing existing skills, or verifying skills work before deployment" -- third person, specific triggers. |
| exclusion clause | fail | No "Do NOT use for..." in the description. |
| adversarial pressure testing | partial | The pattern is taught (RED/GREEN/REFACTOR section) but not evidently applied to this skill itself. No baseline-test record exists. |
| rationalization counter table | fail | (same as block check above) |
| red flags list | fail | Template shown, but no actual red flags list for this skill. |
| control tuning (low freedom) | partial | Some rules use low-freedom phrasing ("ALWAYS run the detection command", "delete the user copy immediately"). Others hedge ("Keep content inline unless it's a large API reference or executable script"). |
| explain-the-why on rules | partial | Shadowing has good explanation; description rule has its own explanation; some rules are imperative without why. |

### Conditionally required patterns

| Pattern | Condition | Fires? | Status |
|---|---|---|---|
| autonomy calibration | IF skill invokes specific tools | no -- only shows snippets, doesn't autonomously invoke | n/a |

### Prohibited patterns

| Pattern | Status | Notes |
|---|---|---|
| high-freedom phrasing | present | "Keep content inline *unless* it's..." softens what should be a rule. "*If* any shadows are found, delete the user-level copy *unless* there's an explicit reason..." -- both have escape hatches. |
| softening hedges in rule statements | present | Same examples. |

---

## Verdict

**Not well-formed** as a discipline-skill under the strict contract:
- 2 of 3 required blocks fail
- 4 of 7 required patterns fail or partial
- 2 prohibited patterns are present (with ambiguity)

The audit also flags this as a **mixed-type skill** under the audit's
mixed-type-check preamble. The skill mixes:
- *Reference-skill content* -- SKILL.md structure, frontmatter rules, file
  organization, skill locations
- *Pattern-skill content* -- the red/green/refactor mental model for skill authoring
- *Discipline-skill content* -- the actual rules (description format,
  inline content, shadow detection)

A skill claiming a single type but doing all three is exactly what the
framework's mixed-type flag exists to surface.

---

## Framework friction observed

Things the audit revealed about the contract itself. This is the most
important output of an audit: friction surfaced here feeds back into
framework versions.

1. **The "teaches a pattern" vs "applies a pattern" distinction needs a
   call-out.** writing-skills *teaches* adversarial pressure testing
   (shows the template) but doesn't *apply* it to its own rules. The
   contract said "adversarial pressure testing" was required for
   discipline-skills, but it could mean either. Resolution: clarify that
   the discipline-skill must have been built using adversarial pressure
   testing -- not just describe it. **Applied in framework v1.**

2. **The hedging prohibition is real-world ambiguous.** writing-skills
   contains hedges that capture legitimate exception cases ("delete the
   user-level copy unless there's an explicit reason to keep it"). The
   exception is real. The original prohibition flagged this as a
   violation. Resolution: distinguish *softening hedges* (weaken the
   rule's core) from *exception clauses* (carve out a known legitimate
   case). **Applied in framework v1.**

3. **"explain-the-why on rules" is hard to enforce universally.** Some
   rules have obvious whys; some are deeper conventions. The required
   row could become "explain-the-why on rules whose rationale is not
   self-evident from the rule statement" but that's vague. Worth
   considering whether this should be conditionally required rather than
   universally required. **Open; revisit when subsequent audits surface
   it as friction.**

4. **The exclusion clause is universally missing in older skills.** The
   pattern is required for every skill type but predates this framework.
   Mass-remediation is needed when older skills are imported.
   **Open; will surface again as more skills are audited.**

5. **Mixed-type skills are likely common.** writing-skills is one. The
   framework's mixed-type-flag rule should probably be promoted from a
   late audit step to a more prominent position. **Applied in framework
   v1: the auditing section now opens with a mixed-type-check preamble.**
