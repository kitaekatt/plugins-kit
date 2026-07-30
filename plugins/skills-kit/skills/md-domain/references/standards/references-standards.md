# Cross-Reference Standards

What good **cross-references** look like across a project's markdown. The
artifact here is not a single file but the reference graph itself: every
`skill: "..."` hard-dependency invocation, every `/example:skill-name` prose
reference, every skill name, and the precedence relationships between skills of
different scopes.

This doc is deliberately thin. Unlike the skill / claude-md / project-doc
standards, cross-reference quality is almost entirely mechanical: a reference
either resolves against the skill pool or it does not. There is no
principle-derived judgment layer to state, and nothing here should be padded
into one.

Both md-domain lanes read this doc: the **audit lane** applies it to detect
broken references, and the **authoring lane** applies it when writing any doc
that names a skill.

## What good looks like

1. **Every hard dependency resolves.** A `skill: "..."` invocation names a skill
   that exists in the resolved skill pool. This is the only cross-reference
   defect that is a runtime failure rather than a reading defect: the code path
   crashes when it fires. Rule id `hard_dep_missing`, severity FAIL.

2. **Soft references are current.** A `/example:skill-name` reference in prose
   resolves against the skill pool. A stale one does not crash anything, but it
   sends the reader (human or agent) to a skill that is not there. Rule id
   `soft_ref_missing`, severity INFO.

3. **Names match their targets.** A SKILL.md's frontmatter `name:` field matches
   the directory the file lives in. A mismatch is usually a rename leftover and
   makes the skill hard to look up. Rule id `name_mismatch`, severity INFO.
   Before correcting one, verify which side inbound references actually use --
   renaming changes the skill's resolvable identity, so the fix must not break
   live references.

4. **No unintended shadowing.** A user-level skill (`~/.claude/skills/<name>`)
   with the same name as a project-level skill overrides it at runtime. That is
   a legitimate pattern when it is deliberate (a personal override) and a defect
   when it is not. Rule id `shadowing`, severity INFO. A precedence relationship
   is never edited without user direction.

## Marking deliberately-non-live references

Some references are correct precisely because they do not resolve: syntax
examples, planned-but-unbuilt skills, and historical records. A compliant doc
marks these explicitly rather than leaving them indistinguishable from rot.

- **Escape prefixes.** When showing example skill-reference syntax in prose, use
  the `/example:` or `/proposed:` prefix (e.g. `/example:skill-name`,
  `/proposed:run-bot`). The scanner ignores any reference carrying one of these
  prefixes and never reports it as broken. `example:` marks a
  meta-descriptive illustration; `proposed:` marks a forward-looking or planned
  skill.

- **Code masking.** Fenced blocks and inline code spans (single/double-backtick
  runs) are masked by the scanner. A slash token that is not a skill reference
  at all -- a CLI flag, a `/route` endpoint, `$/unit` notation, an XML or
  template placeholder -- belongs in a fence or backticks, where it reads
  correctly and fires nothing.

- **Per-file stale allowlist.** For historical artifacts (rollout summaries,
  design plans whose proposed names were later renamed or never built,
  postmortems recording past state), declare the legacy names in YAML
  frontmatter:

  ```yaml
  ---
  references-audit-allow-stale: plan, designer-plan, rollback-to-preflight
  ---
  ```

  Listed bare names are silenced inside that file only, for both soft refs and
  hard deps. Any *new* broken reference in the same file still fires -- the
  allowlist is an explicit exception list, not a file-level bypass. Prefer this
  over rewriting historical references to backticks or escape prefixes when the
  doc's value is the historical record itself. Document the allowlist in an
  editor's note inside the doc, so a reader sees both the declared exceptions
  and the reason for them. The legacy field name
  `audit-references-allow-stale` is still recognized for backward
  compatibility; prefer the current name on any file you touch.

## What is not in this doc

- **Scanner false-positive triage.** Recognizing that a flagged token is a
  compound adjective, a CLI flag, an XML tag, or harness-transcript vocabulary
  is detector-accuracy work, not a statement about what a good cross-reference
  is. Those categories belong to the audit lane's taxonomy, not to these
  standards.

- **Dispositions and buckets.** This doc is bucket-neutral: it says what good
  looks like, not how a finding is dispatched. The references audit lane
  preserves its own legacy AUTO / DISCUSS / SPECIAL bucket vocabulary and its
  category letters verbatim (contract preservation); mapping a rule to a
  disposition happens there.
