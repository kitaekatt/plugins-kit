# CLAUDE.md Audit Criteria

The full criteria for the claude-md-audit member (reached via `/md-audit claude-md`). Organized by cohesion principle (CCP / CRP / ADP) plus universal hygiene. Each criterion has a testable rule and a severity (FAIL / INFO / PASS); decision rules at the bottom.

The placement principles these criteria derive from live in the `cohesion-principles` skill (`plugins/skills-kit/skills/cohesion-principles/SKILL.md`). When the two diverge, cohesion-principles wins; this file gets updated to match.

## Role-to-criteria map

The role of a CLAUDE.md determines which subset of criteria applies. Roles are computed relative to the current working directory.

**Load-trigger note (reachability).** A non-root CLAUDE.md loads on BOTH triggers: cwd descent into its directory AND file access (Read/Edit/Write) beneath it. Data directories and leaf code packages are typically worked by path from a repo-root cwd -- their CLAUDE.md files (review rails for committed-data diffs, package review notes) are REACHABLE and correctly placed. Do not flag such a file as dead weight or its facts for bubbling to root on a cwd-only reachability model; the reader set is "sessions touching files beneath this directory", by cwd or by path (see cohesion-principles `directory_claude_md`).

| Role | Definition | Criteria applied |
|---|---|---|
| `root` | CLAUDE.md at cwd when no CLAUDE.md exists above it -- claude was launched at the project top | CCP (all), CRP (all), ADP (all), Hygiene (all incl. H1/H2/H3) |
| `child` | CLAUDE.md below cwd, OR at cwd when an ancestor CLAUDE.md exists above it (directory-local / subordinate scope) | CCP (incl. parent-child), CRP, ADP, Hygiene (skip H1/H2/H3 -- those belong to root only) |
| `ancestor` | CLAUDE.md above cwd (loaded ambient from the user's session) | CCP (all), CRP (all), ADP (all), Hygiene (all) |
| `local` | CLAUDE.local.md at any directory | CCP only (C-3, C-4); ADP and Hygiene skipped because the file is personal-scoped by design |

### Code-directory dimension (orthogonal to role)

Independently of role, `discover.py` flags each file `dimension: code-directory | classic` (Level-1 trigger; see `scripts/discover.py::classify_dimension`). A `code-directory` file — a per-directory review-notes CLAUDE.md sitting inside code/YAML/CSV — gets the criteria above **plus** the code-directory insight-validation dimension (CD-1…CD-6: anchor-modality classification, fidelity, value filter), which validates that its claims still match the code and still earn their place. Those criteria are self-contained in `references/code-dir-insight-filter.md`; load that doc in addition to this one when, and only when, the file is flagged `code-directory`. A `classic` file (root project file, docs, skill dir, or any file declaring a `claude_md:` block) runs the criteria above only — do not load the insight filter for it.

## CCP findings (write-together / change cadence)

CCP says: content that changes for the same reason belongs in the same file. A CLAUDE.md fact lives where its change driver lives.

### C-1. Parent-child duplication

**Rule:** A child CLAUDE.md must not repeat instructions present in any ancestor CLAUDE.md.

**Why CCP:** the duplicated facts share the same change driver (whatever caused the parent to state the rule causes the child to need updating); SSOT is broken; updates drift between copies.

**Test:** read parent CLAUDE.md content; for each instruction in the child, check whether the same instruction (verbatim or near-verbatim) is in any ancestor. The test is TEXTUAL: verbatim or near-verbatim sentence-level similarity of the instruction/content. An argument that the child "operationalizes" the ancestor's concepts (e.g. as review rails), serves a different reading task, or has a different change driver does NOT exempt a near-verbatim restatement -- if the sentences match, the finding fires regardless of the surrounding framing.

**Severity:** FAIL (each duplicated instruction is a finding).

**Remedy:** delete the duplicate from the child; the ancestor instruction already loads ambient. Exception: a one-line guardrail naming the rule or failure mode may remain per C-5's "never flag instructions that document known agent failure modes" and A-4's "keep a one-line guardrail in CLAUDE.md naming the error; the skill carries the depth" -- multi-sentence restatement of mechanics/detail still fails, so the remedy for such lines is trim-to-guardrail-plus-pointer, not deletion.

### C-2. Sibling duplication

**Rule:** When the same instruction appears in multiple sibling CLAUDE.md files, it belongs in their common ancestor.

**Why CCP:** sibling duplication is a CCP signal that the change driver is shared; a single ancestor placement is the SSOT remedy.

**Test:** when auditing multiple files, scan for instructions present in 2+ sibling files. Flag instructions that share the same parent.

**Severity:** FAIL (instruction duplicated across siblings).

**Remedy:** move the instruction to the common ancestor; remove from siblings.

### C-3. Personal-vs-shared cadence (CLAUDE.local.md only)

**Rule:** CLAUDE.local.md contains only machine-specific paths, personal preferences, and individual overrides. Team-useful content belongs in the shared CLAUDE.md.

**Why CCP:** personal preferences change at a different cadence than team conventions; mixing them forces shared-file edits when only personal preferences shift.

**Test:** for each instruction in the .local file, ask "would another team member benefit from this fact?" If yes, flag.

**Severity:** FAIL on team-useful content in .local.

**Remedy:** move team-useful content to the shared CLAUDE.md.

### C-4. Local duplication of shared (CLAUDE.local.md only)

**Rule:** CLAUDE.local.md must not repeat instructions from the shared CLAUDE.md.

**Why CCP:** same SSOT violation as C-1, applied to the local-vs-shared pair.

**Test:** diff against shared CLAUDE.md.

**Severity:** FAIL on duplications.

### C-5. Content earns its place

**Rule:** Each instruction passes the test "Would removing this cause the agent to make a mistake?" If not, it is a non-load-bearing fact and should be removed or moved to a deferred reference.

**Why CCP:** a fact with no change driver in this scope's directory has no reason to live here; nothing local would cause it to update. Same-cadence content with no driver is decay.

**Test:** for each instruction, ask:
1. Would removing this cause the agent to make a mistake in a typical session in this scope?
2. Is the fact already injected by the system (e.g. skill names auto-listed), making the local copy redundant?

**Critical exception:** common agent error patterns (things the agent repeatedly gets wrong) MUST stay in CLAUDE.md even if they could theoretically live in a skill. Skill invocation is not reliable enough to gate error-prone behaviors behind. Never flag instructions that document known agent failure modes.

**Severity:** FAIL on non-load-bearing instructions outside the exception.

### C-6. Project-reference duplication of skill content

**Rule:** A CLAUDE.md must not embed (or cite a project reference doc that embeds) content that already lives in a skill.

**Why CCP/SSOT:** when a skill exists for a topic, the skill's references/ folder is the SSOT. A CLAUDE.md that duplicates skill content (inline or via a parallel project reference doc) creates two copies that drift independently.

**Test:** for each substantial block of CLAUDE.md content (or each project-reference doc cited from CLAUDE.md), check whether a skill exists for the same topic. If yes, the content should collapse to a pointer (`for X, invoke /example:skill-name`) or, where the harness supports it, a `required-skills:` declaration. The test is TEXTUAL: verbatim or near-verbatim similarity of the instruction/content against the skill's own text. An argument that the CLAUDE.md block "operationalizes" the skill content for a different reading task or change driver does NOT exempt a near-verbatim restatement -- if the sentences match, the finding fires.

**Severity:** FAIL on duplicated skill content. INFO when the project-reference predates the skill and graduation work is in progress. Exception: a one-line guardrail naming the rule or failure mode may remain per C-5's "never flag instructions that document known agent failure modes" and A-4's "keep a one-line guardrail in CLAUDE.md naming the error; the skill carries the depth" -- multi-sentence restatement of mechanics/detail still fails, so the remedy for such lines is trim-to-guardrail-plus-pointer, not deletion.

### C-7. No field-by-field restatement of a code-enforced contract

**Rule:** For a machine contract that code validates (a schema enforced by a validator module, a wire format enforced by a parser), the validating code's in-code doc (module docstring / contract comment) is the SSOT for the field-level detail. A CLAUDE.md states the contract's existence, its invariants, and its change-discipline ("the schema changes in the same diff as the validator"), and cites the module for the fields. It must not enumerate the schema field-by-field.

**Why CCP/SSOT:** the in-code doc changes in the same diff as the validator (perfect CCP); an md copy of the field list changes in a different diff and drifts while still looking authoritative.

**Test:** for each schema/format the file describes, check whether code validates it; if yes, check whether the file restates the field list rather than citing the module.

**Severity:** FAIL on live field-by-field restatement; INFO when the restatement is explicitly flagged in-doc as an accepted drift risk.

## CRP findings (read-together / smallest reader-set)

CRP says: a fact lives in the smallest scope whose readers all need it. Readers of this scope should plausibly need every fact in the scope.

### R-1. Directory-appropriate content

**Rule:** Content only relevant when working in a specific subdirectory should live in that subdirectory's CLAUDE.md, not at a higher scope.

**Why CRP:** a fact that does not fire for a typical reader of this scope should bubble down to the scope where every reader needs it.

**Test:** for each instruction, ask "would an agent working in a sibling part of this scope need this?" If no, the fact belongs closer to the code it describes.

**Severity:** INFO (migration opportunity; not always wrong -- poor directory organization sometimes requires explanations at a higher level).

### R-2. Self-contained context (CRP within file)

**Rule:** Each CLAUDE.md must be understandable without reading any document other than its ancestor CLAUDE.md files (which are always loaded). Project-specific terminology (system names, API names, acronyms) must be established before reference.

**Why CRP:** sections within the file form their own internal load order. A reader hitting a project-specific term must have already encountered its identity; otherwise the reader cannot use the surrounding instructions.

**Test:** scan for project-specific terms; for each, verify a one-line identity establishment occurs at first use (in this file or an ancestor).

**Severity:** FAIL on terms used without prior identity.

**Remedy:** add a one-line identity ("X is the Y system") at first use.

### R-3. Size signals (CRP evaluation prompt, not verdict)

**Rule:** Files that exceed 500 lines / 3000 tokens deserve a CRP evaluation, not an automatic split.

**Why CRP:** the threshold is a signal that the file may have accumulated multiple reading tasks. Splitting is correct ONLY if a CRP-passing decomposition exists -- sections must serve different reading tasks. A stub-with-always-co-loaded reference is CRP-fail (tool-call doubling without context-efficiency win); see `cohesion-principles` and framework.md "CRP is the test for L2 -> L3 splits."

**Test:**
1. Count effective lines (excluding trailing blanks).
2. If > 200 (root) or > 60 (child) ideal: emit INFO finding.
3. If > 500 lines or > 3000 tokens: emit INFO recommending a CRP evaluation, with explicit warning that splitting is only legitimate when sections serve different reading tasks.

**Severity:** INFO at all sizes (size alone is never FAIL).

### R-4. Progressive-disclosure opportunities

**Rule:** When a file exceeds size ideals, identify content that could legitimately move to one of four destinations, in order of preference:

1. **A skill** (SKILL.md + structured contract) -- if the content fits a skill type (procedure -> technique-skill; rule + counter -> discipline-skill; lookup table -> reference-skill; tool/MCP/API wrapper -> capability-skill). This is the highest-leverage destination: discoverable trigger, audit surface, typed contract.
2. **A skill's references/ folder** -- if the content already belongs to an existing skill but lives inline in CLAUDE.md by accident. Cite via `for X, invoke /example:skill-name`.
3. **A project reference doc** (a markdown file outside any skill, e.g. `<project>/docs/<topic>.md` or `.claude/docs/<topic>.md`) -- the escape hatch when the content does not yet fit a skill type but is too large for inline. Useful for emerging concepts that may eventually graduate into a skill (see "Skill-maturation pipeline" in `cohesion-principles`).
4. **A child CLAUDE.md** (loaded lazily when agent enters that directory) -- if the content is directory-specific and serves the in-directory editor reader.

**Maturation flag:** when identifying a project-reference destination, also check whether the content has matured into a structured shape that fits a skill type. If yes, recommend graduation into a skill rather than placement as a project reference.

**Critical exclusions** (never flag for migration):
- Common agent error patterns (must stay inline; gating behind a skill is C-5 / A-4 territory).
- Build commands (needed on nearly every session).
- Gotchas / traps (highest-value content; keep prominent).

**Severity:** INFO (each migration candidate is a separate finding).

## ADP findings (link-forward-only / DAG)

ADP says: file references run downward in load order. Each file may cite earlier-loaded files; later-loaded files must not be cited as load dependencies.

**Legitimate forward edges from a CLAUDE.md** (not flagged by these rules):
- CLAUDE.md -> project reference doc, e.g. `for migration patterns, see docs/migration-guide.md when working on database changes`. The pointer is informational; CLAUDE.md instructions remain complete without the reference being loaded.
- CLAUDE.md -> skill via prose pointer, e.g. `for any Python work, invoke /python-coding`. The pointer names a downstream skill the agent should invoke.
- CLAUDE.md -> skill via YAML header, e.g. `required-skills: [python-coding]`. Where the harness supports it, this declares a skill that should be auto-loaded when the CLAUDE.md is in scope. Confirm harness support before relying on the field.

The criteria below flag the prohibited cases.

### A-1. Referenced documents exist

**Rule:** Every cross-file reference (`see X`, `refer to X`, `documented in X`, paths in instructions) must resolve to a file that exists.

**Why ADP:** a broken edge breaks the DAG; the agent has no path to the cited content.

**Test:** for each path-like reference, use Glob or filesystem check to verify existence.

**Severity:** FAIL on missing references.

### A-2. No `@import` of large content

**Rule:** `@import` (lines starting with `@path/to/file`) inlines the imported file at session start -- it is NOT lazy. Files over ~50 lines imported this way should use deferred references instead.

**Why ADP:** `@import` collapses the load graph by inlining; large imports inflate L1 with content that should be at L2 or L3.

**Test:** scan for lines matching `^@`; for each, check the imported file's size; flag imports of files > 50 lines.

**Severity:** FAIL on large `@import`.

**Remedy:** replace `@path/to/file` with prose: "See path/to/file when working on X."

### A-3. Stale references

**Rule:** References to files, commands, sections, or patterns that no longer exist break the DAG.

**Why ADP:** broken edges. Includes file paths (overlap with A-1) and non-file references like CLI flags, class names, internal section headers.

**Test:** check internal section-header references against the file's actual headers; check CLI flag references against the project's tooling; check class-name references against grep results.

**Severity:** FAIL on stale references.

### A-4. Skills gating common errors

**Rule:** A common agent error pattern must be reachable from CLAUDE.md (always-loaded layer), not gated solely behind a skill's trigger.

**Why ADP:** skill invocation is conditional on the description matching the user's request; common errors fire in many contexts that may not trigger the skill. Gating a common error solely behind a skill creates a load-graph dependency on a trigger that may not fire.

**Test:** scan for `for X, see /Y` style pointers; for each, judge whether the underlying fact is a common agent error. If it is, the fact (or at least a one-line guardrail) must be inline in CLAUDE.md.

**Severity:** FAIL on common-error gating.

**Remedy:** keep a one-line guardrail in CLAUDE.md naming the error; the skill carries the depth.

### A-5. Parent-to-child citation as load dependency

**Rule:** A parent CLAUDE.md must not cite a child CLAUDE.md by name as required content.

**Why ADP:** the child loads conditionally on cwd; sessions where the child does not load see an incomplete parent. The parent's correctness must not depend on a downstream load.

**Permitted:** the parent may say "for X-specific work, see <X>/CLAUDE.md" as an orientation pointer, IF the parent's instructions are complete without the child being loaded.

**Test:** scan for `see <subdir>/CLAUDE.md`-style references; for each, check whether the parent's instruction is incomplete without the child.

**Severity:** FAIL on incomplete-without-child references.

## Hygiene findings (universal)

Not derived from cohesion principles -- agent-fluency rules retained because the failure modes are common and the remedies are mechanical.

### H-1. Project identity (root only)

Root CLAUDE.md includes a brief project description (what it is, tech stack). 1-3 lines.

Severity: FAIL if missing.

### H-2. Essential commands (root only)

Root CLAUDE.md presents build / test / lint commands as exact runnable commands, not prose.

Severity: FAIL if missing for a project that has builds/tests.

### H-3. Directory structure (root only)

Root CLAUDE.md includes a high-level directory map showing where major components live.

Severity: FAIL if missing for a multi-component project.

### H-4. Prohibitions have positive alternatives

Every prohibition ("never use X", "don't do X") includes a positive alternative ("instead use Y", "prefer Z"). An agent with no path forward will ignore the rule or get stuck.

Test: scan for negation patterns; verify each has a corresponding "instead", "prefer", or "use Y".

Severity: FAIL on prohibitions without alternatives, with the documented exception of cases where there is no safe alternative (e.g. "never `p4 obliterate` -- destroys history" needs no alternative).

### H-5. No personality instructions

No "be a senior engineer", "act as an expert", "you are a helpful assistant" or similar. The model already reasons at expert level; these waste tokens.

Severity: FAIL on personality directives.

### H-6. No generic programming advice

No instructions the model would follow without being told (e.g. "write clean code", "use meaningful variable names", "handle errors appropriately").

Test: would Claude do this anyway without the instruction? If yes, FAIL.

Severity: FAIL on generic advice.

### H-7. Gotchas are specific and actionable

Gotcha entries describe a concrete failure mode AND how to avoid it. Vague warnings ("be careful with X", "watch out for Y") fail this criterion.

Severity: FAIL on vague gotchas.

### H-8. No linter-enforced style rules

Style rules that a linter / formatter already enforces are noise, unless documenting a common agent error the linter cannot catch.

Severity: FAIL on linter-redundant style rules.

### H-9. No embedded documentation

Exhaustive API docs, architecture deep-dives, or long reference tables that should be deferred references.

Boundary -- deferred-reference pointer lists: a list of pointers to deferred references is not embedded documentation. The annotation ceiling is one "read when ..." line per pointer; annotating beyond that (multi-line summaries per pointer) re-embeds the documentation and trips this criterion.

Severity: FAIL on embedded documentation > 30 lines that has no agent-error driver.

### H-10. No unpruned auto-generation

Signs of `/init` output never edited: boilerplate headers ("This file provides guidance..."), placeholder sections, obvious filler.

Severity: FAIL on visible auto-generation artifacts.

### H-11. Obeys ancestor-declared conventions

An audited file must not violate a convention that an **ancestor CLAUDE.md explicitly declares**. Ancestor CLAUDE.md files -- every CLAUDE.md above the subject on the directory path up to the workspace root -- load ambient in any session that touches the subject, so their stated conventions bind the subject exactly as they bind any file in their scope. Typical declared conventions: ASCII-only mandates ("never write non-ASCII characters into source files"), "no absolute paths in shared files", required formatting or structure rules the project's CLAUDE.md states.

**Rule-extraction posture (mirrors the code-review reviewer_a):** a violation may be flagged ONLY when the exact declared rule can be **quoted verbatim** from an ancestor CLAUDE.md. No inferred conventions, no generic best-practice, no "the spirit of" a rule, no convention you believe is standard but the ancestor did not write down. If you cannot quote the ancestor's rule text verbatim, do not raise the finding.

**Anchor + message:** the finding anchors on the SUBJECT file (the line that violates the rule), and its message carries (a) the **verbatim ancestor rule quote** and (b) the **source path** of the ancestor CLAUDE.md that declared it -- so the author can see both what was violated and where the rule lives.

**Scope:** fires only when ancestor CLAUDE.md files are supplied to the audit (the `ancestorClaudeMdPaths` argument, nearest-ancestor first). A `root`-role file with no ancestors, or a run that supplies no ancestor paths, emits no H-11 findings.

Taxonomy: `R_ancestor_convention_violation` (group Hygiene). Disposition is assigned instance-level by the classifier like any other convention-violation fix -- normally **FIX** (a mechanical correction against a documented project convention: replace a non-ASCII look-alike, relativize a hardcoded absolute path, apply the stated formatting rule), and **SERIOUS** when the violation reveals a real-world problem the ancestor's rule exists to prevent (e.g. a committed secret an ancestor forbids).

Severity: FAIL on a verbatim-quotable ancestor-convention violation.

## Output format

### Per-file report

```
## <file path> (<role>)

Lines: <N> (<size assessment relative to ideals>)

### CCP findings (write-together / change cadence)
[PASS] C-1: no parent-child duplication detected
[FAIL] C-5: line 47 instruction "X" has no change driver in this scope
  Recommendation: move to <suggested-scope> or remove

### CRP findings (read-together / smallest reader-set)
[INFO] R-3: 426 lines exceeds 200-line acceptable ceiling
  Opportunities: section "Y" (32 lines) could be a deferred reference doc

### ADP findings (link-forward-only / DAG)
[FAIL] A-1: reference "Code Standards" (line 143) does not resolve to any header in this file
  Recommendation: rename to "Development Guidelines" or add the missing header

### Hygiene findings (universal)
[PASS] H-4: prohibitions all carry positive alternatives
[FAIL] H-10: line 3 contains unpruned `/init` boilerplate

### Schema validation (if claude_md: YAML block present)
[PASS] yaml: schema validation -- all required keys present, all rules satisfied

### Compliance

<P> PASS | <F> FAIL | <I> INFO
COMPLIANT | NON-COMPLIANT
```

### Overall report (multiple files)

```
## Audit Summary

Files audited: <N>
  COMPLIANT: <file list>
  NON-COMPLIANT: <file list>

Top recommendations:
1. <highest-priority actionable item>
2. ...
```

### Decision rules

- Any FAIL finding -> file is NON-COMPLIANT.
- Only PASS and INFO findings -> file is COMPLIANT.
- INFO findings are advisory improvements, not compliance failures.
- INFO findings do not escalate to FAIL on subsequent runs even if unaddressed.
