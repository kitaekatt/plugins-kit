# Finding Taxonomy and Remediation

> Reference doc owned by `md-domain`'s `audit_references` lane, folded in from the
> former standalone `references-audit` skill at the phase-3 restructure. Preserved
> content below is byte-faithful except path updates for the new location.

Load this when you are interpreting a references-audit report and deciding how to fix each finding. The scanner's job is detection; the classification and remediation here is inference work -- that's why it lives in a skill, not in the script.

## YAML-transcription note

When authoring an audit-skill and transcribing category descriptions, detection signals, remediations, or examples from this document into the skill's YAML body (notably the `taxonomy:` list), any string containing backticks, asterisks, or square brackets must be double-quoted in YAML. Plain (unquoted) scalars with backticks fail to parse with `found character '\`' that cannot start any token`. For short text with internal backticks, use double-quoted strings and escape any internal `"` as `\"`. For longer remediation text spanning multiple sentences, prefer the YAML folded block scalar `>-` (which strips newlines) or literal block scalar `|-` (which preserves them). Example: the category A detection signal `WARNING '/example:old-name'` becomes `detection_signal: "WARNING '/example:old-name'"` in YAML, while a multi-sentence remediation becomes:

```yaml
default_remediation: >-
  Mechanical find/replace of the old name with the new name within
  the file. If the surrounding sentence describes old behavior, also
  update the prose so it matches the new skill.
```

## The disposition model (ratified four-disposition contract)

Every finding gets a taxonomy category (A-K) AND one of four dispositions. This is the ratified four-disposition contract that replaced the earlier three-bucket dispatch model. The authoritative spec is the four-disposition contract in [`audit-framework.md`](audit-framework.md); this doc applies it to references-audit findings.

```
report -> classify each finding -> disposition -> dispatch
                                        |
              +----------+----------+----------+----------+
              v          v          v          v          v
            FIX      SERIOUS     IMPROVE     SILENT     SPECIAL
       (auto-apply) (top of    (count +    (not       (K escape
                     report)   one-liners, surfaced)   hatch)
                                opt-in)
```

- **FIX** -- auto-applied; lands in a reviewable CL. Anything decidable by VERIFIED FACTS (does the ref resolve against the scanner's skill pool?) plus DOCUMENTED PROJECT CONVENTIONS (the `example:` / `proposed:` escape prefixes, code-fence masking, ASCII-only). The bar: would a reasonable owner accept this diff in CL review without discussion? "Very likely improving" clears it.
- **SERIOUS** -- surfaced at the TOP of the report, summarized, NEVER auto-fixed, never buried. A hard-dependency invocation to a genuinely-gone skill with no replacement and no mechanical escape: a live runtime-crash path with no surviving mechanism. The real finding is the unguarded invocation, not the doc drift.
- **IMPROVE** -- count + one-line pitches, discussion opt-in. Structural or judgment calls where no fact and no convention decides the fix.
- **SILENT** -- not surfaced at all, no hedging. Accepted structural patterns (an intentional personal-override shadow), do-nothing conclusions, findings the scanner already silenced via `references-audit-allow-stale`.
- **SPECIAL** -- category K only, the escape hatch.

Classifier prod (read this FIRST -- it overrides default caution): You are biased toward conservatism; the user's time and attention are the scarce resources; source control and CL review are the safety net. If the edit very likely improves the doc, apply it.

For each finding produced by the scanner (markdown or JSON output):

1. **Classify** into exactly one category A-J below. If none fit, classify as **K (unclassified)**.
2. **Assign a disposition** instance-level against the predicates above (the per-category default below is a starting point only).
3. **Dispatch**: FIX applies by definition (no decision); IMPROVE + SPECIAL go into ONE foreground Q&A round; SERIOUS is surfaced summarized at the top; SILENT is omitted. Do not block FIX on the IMPROVE conversation.
4. After remediation returns, re-run the audit. Iterate only on newly-surfaced findings.

Ambiguity rulings: (1) Your own verified reading (the ref does / does not resolve; the prose is / is not meta-descriptive) DISCHARGES any "confirm with author" hedge -- a decided FIX must not leak back into IMPROVE. (2) A false-positive/detection artifact that is ALSO a genuine convention fix (a real broken ref, not just scanner noise) is FIX, not SILENT. (3) A mechanical fix never waits on a larger structural relocation -- FIX the mechanical part now; the relocation stays a separate IMPROVE.

## Scanner-rule dispositions (default per rule, before taxonomy refines)

references-audit is a corpus-wide scanner with four rules. The taxonomy A-K above refines a `soft-ref` / `hard-dep` finding's disposition once the *why* is known; but each rule also has a sensible default, derived from the master razor:

| Rule | Severity | Default disposition | Rationale |
|---|---|---|---|
| `hard-dep` | FAIL (ERROR) | **FIX** if a mechanical re-point/prefix resolves it (known rename, `example:`/`proposed:` escape); **SERIOUS** if the invoked skill is genuinely gone with no replacement and no escape | A `skill: "..."` invocation is executable. When a mechanical fix decides it, that fix is FIX; when nothing resolves it, it is a live runtime-crash path with NO surviving mechanism -- the ratified SERIOUS predicate (a protective/functional mechanism that is fictional), surfaced at the top, never buried, never auto-fixed. |
| `soft-ref` | INFO (WARNING) | **FIX**, refined per taxonomy | A prose `/skill` ref is decidable against the verified skill pool + the documented escape-prefix / code-fence conventions; most categories (A known, C, E, F, G, I, J) are mechanical. Retired (B non-incidental), scope (D), and harness (H) refine to IMPROVE. |
| `name-mismatch` | INFO (WARNING) | **FIX** (align frontmatter `name:` and directory), else **IMPROVE** | A rename leftover is a consistency fix against the verified directory name -- decidable. But renaming changes the skill's resolvable identity: verify which side inbound refs actually use (loss-free guard) first; if inbound refs disagree on the canonical name, renaming would break real refs, so IMPROVE. |
| `shadowed` | INFO | **IMPROVE**, **SILENT** when intentional | The scanner cannot infer intent, and a precedence relationship is never auto-edited. An accidental shadow is worth one opt-in one-liner ("user skill X shadows project skill X -- intended?"); a confirmed intentional personal override is an accepted structural pattern -- SILENT. |

---

## Categories

### A. Renamed skill (1:1 replacement exists)

- **Detection signal.** WARNING `/example:old-name` (or ERROR `skill: "example:old-name"`); a current skill `/example:new-name` clearly covers the same responsibility (confirmable from upstream CHANGELOG, the new skill's description, or an explicit "renamed from" line).
- **Disposition.** FIX when the 1:1 mapping is known (decidable against the skill pool + a "renamed from" fact). IMPROVE otherwise -- offer the best-guess new name as a one-liner once for the whole audit ("I see refs to `/example:old-name`. Best guess `/example:new-name`. Apply?").
- **Default remediation.** Mechanical find/replace of the old name with the new name within the file. If the surrounding sentence describes old behavior, also update the prose so it matches the new skill.
- **Example (CL 147036).**
  ```
  - references using this prefix are not flagged as `/skill-deps`
  + references using this prefix are not flagged as `/references-audit`
  ```

### B. Retired/deleted skill (no replacement)

- **Detection signal.** WARNING `/example:old-name`; no current skill covers the responsibility. The reference is often the subject of a whole section or paragraph.
- **Disposition.** IMPROVE by default -- the delete-vs-rephrase sub-case (delete the section, demote to backtick, or add to `references-audit-allow-stale`) is a structural judgment that protects surrounding true content; the loss-free-deletion guard applies. FIX only in the purely-incidental sub-case (2 below): a broken clause whose removal loses nothing is falsified-content deletion.
- **Default remediation.** Four sub-cases, picked by structural context:
  1. Reference is the **subject of a whole section/paragraph** -> delete the section.
  2. Reference is an **incidental clause** (e.g. "similar to the old skill") -> delete the clause, keep the surrounding sentence.
  3. Reference is **historical context inside a doc that mixes live and stale names** (e.g. "previously known as ...") -> demote to backticked literal (`` `old-name` ``).
  4. **The whole document is a historical artifact** (rollout summary, design plan recording past intent, postmortem) where consistent backtick-demotion would either be noise or destroy the historical record -> add the legacy names to the file's `references-audit-allow-stale` YAML frontmatter and write an editor's note at the top explaining current state. Leave the slash refs in place. This preserves typography parity with the doc's other still-live references and keeps the audit honest: a *new* broken ref in the same doc still fires.
- **Example (CL 147036).** dialog-domain referenced the deleted `dialog-experiments` skill; the whole "External Analysis Tools" section was removed. **Example (allow-stale).** A rollout summary describing 2026-Q1 work lists `/plan`, `/preflight`, `/swarm submit` in a single bullet. `/plan` was later merged into `/designer-plan-domain`; the others still resolve. Demoting just `/plan` would produce inconsistent typography; adding `plan, designer-plan` to the file's `references-audit-allow-stale` plus a one-line editor's note silences the audit without rewriting the historical record.

### C. Merged skill (subskill folded into parent)

- **Detection signal.** WARNING `/example:parent-sub`; a current skill `/example:parent` exists; release notes or SKILL.md document the merge.
- **Disposition.** FIX. A prose rewrite (`/example:parent-sub` -> `/example:parent sub`) and a dispatch-alias-table demote-to-backtick are both mechanical convention fixes decidable from the documented merge -- the table sub-case just applies a different remediation (backtick the literal instead of converting to the dispatch form).
- **Default remediation.**
  - In prose: rewrite the slash form (e.g. `/example:parent-sub`) to the new dispatch form (`/example:parent sub`).
  - In dispatch alias tables / synonyms lists: keep the literal name in backticks (e.g. `` `parent-sub` ``), not as a slash reference. The skill code may still accept the legacy literal as a synonym; the reference shouldn't look like a callable skill.
- **Example (CL 147036).**
  ```
  - Via `/playtest preflight` (or the legacy `/playtest-preflight`) for standalone validation
  + Via `/playtest preflight` (or the legacy `playtest-preflight` argument) for standalone validation
  ```

### D. Scope-violating cross-reference (project <-> personal)

- **Detection signal.** WARNING `/example:ref-name`; the referenced skill **exists** but in the opposite scope (project skill referencing a personal skill, or a shipped plugin skill referencing a project-only skill). The scanner reports it as missing because the resolver respects scope boundaries.
- **Disposition.** IMPROVE by default -- deleting a cross-scope reference may drop true comparison content, so the removal is a structural judgment (loss-free guard). FIX only when the cross-reference is an incidental, purely-misleading "vs ..." mention whose removal loses nothing.
- **Default remediation.**
  - **Project / plugin -> personal:** delete the cross-reference. A shipped skill cannot assume the reader has the personal skill installed.
  - **Personal -> project:** usually fine; only flag if the personal skill is meant to be portable.
- **Example (CL 147036).** project-scoped `claude-feedback` SKILL.md had a "Key Differences vs /retro" section; `/retro` is a personal skill, so the section was deleted entirely.

### E. Compound-adjective false positive

- **Detection signal.** WARNING `/example:word-foo`; the literal text contains `X-/Y-thing` (compound adjective with embedded slash) or other prose where a slash appears as punctuation, not as a skill reference.
- **Disposition.** FIX (a false positive; rewording loses nothing).
- **Default remediation.** Reword the prose to eliminate the slash. Preserve technical meaning -- the rewrite is "express the same idea differently", not "escape the scanner".
- **Example (CL 147036).**
  ```
  - 'Slack file downloads are bot-/user-token-gated.'
  + 'Slack file downloads are gated by bot or user token scopes.'
  ```

### F. Non-skill CLI flag false positive

- **Detection signal.** WARNING `/example:flag-name`; surrounding text is a shell or CLI invocation (binary name + flags). Common with MSBuild, `devenv`, `cl.exe`, the linker, and other Windows-native tools.
- **Disposition.** FIX (a false positive; fencing loses nothing).
- **Default remediation.** Wrap the whole command in a fenced code block (```` ``` ````), or backtick a single slash-token inline (`` `/flag` ``). The scanner masks both fenced regions and inline code spans, so refs inside them produce no findings.
- **Example.**
  ```
  - Run: devenv /debugexe "...exe" /minidump "...dmp"
  + Run:
  +
  + ```
  + devenv /debugexe "...exe" /minidump "...dmp"
  + ```
  ```

### G. XML / template placeholder false positive

- **Detection signal.** WARNING `/example:tag-name`; surrounding text contains XML or HTML closing tags (such as `</example:foo>`) or template placeholders inside angle brackets.
- **Disposition.** FIX (a false positive; fencing loses nothing).
- **Default remediation.** Same as F -- wrap the XML or template example in a fenced code block. Same scanner masking applies.

### H. Harness transcript false positive

- **Detection signal.** Many WARNINGs in the same file or directory; references match Claude-harness vocabulary (`/example:command-args`, `/example:system-reminder`, `/example:task-id`, `/example:tool-use-id`, `/example:command-name`, `/example:command-message`, etc.).
- **Disposition.** IMPROVE -- a batch config decision. Recommend the `--ignore-dir` wrapper flag ONCE for the whole batch as a one-liner; it edits the invocation wrapper (out of the scanned files), so the user confirms the mechanism and location.
- **Default remediation.** Add the directory to the scanner's `--ignore-dir` flag in the project's invocation wrapper. If the transcripts are project-specific, also commit the wrapper invocation (or document the recommended flags in the host project's CLAUDE.md).
- **Example.** Adding `--ignore-dir 'ClaudeFeedback'` removes ~30 spurious warnings from a session-log archive in one config entry, with no per-file edits.

### I. Illustrative example in a design doc

- **Detection signal.** WARNING `/example:foo` **or** ERROR `skill: "example:foo"`; the surrounding sentence is describing skill-reference syntax in the abstract -- the doc is *about* references, not *making* one.
- **Disposition.** FIX when the surrounding sentence is clearly meta-descriptive ("a `/example:skill-name` reference looks like...") -- your verified reading of the prose discharges the hedge. IMPROVE when the reference sits inside a live procedure or playbook (it could be a real, currently-broken instruction rather than an example).
- **Default remediation.** Add the `example:` prefix to the slash-form, and likewise to any `skill: "..."` hard-dep literal. Both are documented escape prefixes that the scanner ignores.
- **Example.**
  ```
  - soft references (`/name` in documentation text that mislead)
  + soft references (`/example:name` in documentation text that mislead)
  ```

### J. Forward-looking / proposed skill

- **Detection signal.** WARNING `/example:foo`; no current skill named `foo`; surrounding prose frames it as "planned", "future", "we should build", "today: <legacy approach>".
- **Disposition.** FIX when the prose explicitly cues "planned" / "future" / "proposed". IMPROVE if it's ambiguous (could be a real ref to a deleted skill rather than an aspirational plan).
- **Default remediation.** Add the `proposed:` prefix to the slash-form (a documented escape prefix). Optionally append a one-line "(planned, not built)" note if the context isn't already explicit.

### K. Unclassified / special case

- **Detection signal.** None of A-J fit cleanly after a deliberate attempt.
- **Disposition.** SPECIAL.
- **Default remediation.** Surface the finding to the user with: the report line, what you tried to match, why none of A-J fit. The user decides the strategy. If the strategy generalizes, propose it as a new category and add it to this doc in a follow-up.

---

## Background-agent brief template

When the FIX bucket is non-empty, its edits are applied in the REMEDIATE phase, never during classification. For a single affected file the main agent applies them inline with Edit; for two or more files they are handed to `workflow/references-remediate.js` lanes (one per file). The per-finding brief below is fully self-contained -- the executor does not reclassify, it applies. Use this template to build each lane's payload:

> **Task: apply references-audit FIX edits.**
>
> Audit-references identified N broken cross-references in this project. The classification and remediation has been done already; your job is to apply the listed edits exactly as specified. Do not reclassify; do not invent new fixes; do not touch files outside the list.
>
> **Per-finding payload** (one block per fix):
>
> - File: `<absolute or project-relative path>`
> - Line: `<1-indexed line number from the scanner>`
> - Category: `<A | B (incidental clause) | C | E | F | G | I | J>`
> - Before (exact text to match): `<single-line or short snippet>`
> - After (exact replacement): `<single-line or short snippet>`
>
> **Authority and constraints**:
>
> - You may modify any file in the per-finding list. Honor the host project's version-control gate (e.g. `p4 edit` on Perforce projects, `git add` on git) before editing.
> - You may NOT submit, push, or otherwise publish the changes.
> - You may NOT touch files outside the per-finding list.
> - If a finding's "Before" text does not match the file (file changed since classification), skip that finding and surface it back. Do not guess a replacement.
>
> **Return contract**:
>
> 1. The list of findings successfully applied (file + line + category).
> 2. The list of findings skipped, each with the reason.
> 3. Any newly-noticed issues that fall outside the brief (do not act on them).
>
> Return this as a short structured report. The main agent will re-run references-audit after you return and reconcile any remaining findings.

The main agent constructs the per-finding payload by:

- Reading the JSON output from `references_audit.py --json`.
- For each FIX before/after finding, computing the **exact before-text** by reading the cited file at the cited line.
- Computing the **after-text** per the category's default remediation above.
- Bundling all payloads into the single Agent call.

This keeps inference (classification, remediation strategy) on the main agent and execution (apply edits) in the REMEDIATE phase -- inline for a single file, one `workflow/references-remediate.js` lane per file for two or more. The remediate lanes are cheap to parallelize against the foreground IMPROVE/SPECIAL conversation. SERIOUS findings are surfaced summarized at the top of the report and are NEVER handed to a remediation lane.

---

## Foreground Q&A pattern (IMPROVE + SPECIAL)

Batch every IMPROVE and SPECIAL finding into one user-question round (SERIOUS is surfaced separately at the top, summarized, and is not part of this opt-in round; SILENT findings are omitted). Render as a numbered list, each item showing:

- The scanner's report line (file + line + ref).
- The category letter and rationale.
- The inferred options (e.g. "delete section / rewrite clause / demote to backtick" for category B).
- Your recommendation.

The user answers in one pass. Anti-pattern: per-finding round-trips. Anti-pattern: asking the user to gate the FIX bucket on the IMPROVE decisions -- the two are independent.

---

## When this taxonomy needs to grow

If a finding lands in category K (unclassified) and the user's chosen strategy generalizes, propose a new category. Criteria for adding:

- The detection signal is recognizable from the scanner's output without the agent having to re-read the file.
- The remediation can be expressed as a default that applies to the majority of instances in the new category.
- The category is **mutually exclusive** with A-J. If a finding can fit two existing categories, refine the detection signal of one of them rather than adding a new one.

The taxonomy is closed-world only for the scanner's current detection capabilities. As the scanner gains the ability to detect new kinds of staleness (e.g. broken file paths, dead URLs, orphaned references in `Skill: { name: ... }` blocks the regex currently misses), new categories will be added here.
