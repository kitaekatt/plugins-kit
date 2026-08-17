# CLAUDE.md standards

The artifact-keyed standards doc for **CLAUDE.md / CLAUDE.local.md**: what a good one looks like. It is the single source both md-domain lanes read -- the **audit lane** applies it in the detect direction (find violations, classify findings), the **generation lane** applies it in the produce direction (write a file that satisfies it). One standard, two directions.

The placement principles these standards derive from live in `../cohesion-principles.md`. When the two diverge, cohesion-principles wins; this file gets updated to match.

Lanes load THIS doc, not cohesion-principles: the standards below are self-contained so a lane never has to load the upstream spine at runtime.

## Contents

1. [Artifact identity](#1-artifact-identity) -- what the artifact is, its roles and dimensions
2. [Classic standards](#2-classic-standards) -- C-1..C-7, R-1..R-4, A-1..A-5, H-1..H-11
3. [Code-directory dimension](#3-code-directory-dimension) -- CD-1..CD-6 plus the generation shapes
4. [Density lens](#4-density-lens-opt-in-advisory-only) -- DD-1..DD-4, opt-in and advisory only
5. [Audit-direction mapping](#5-audit-direction-mapping) -- criteria ids and the finding taxonomy
6. [Generation-direction notes](#6-generation-direction-notes) -- the `claude_md:` block shape

---

## 1. Artifact identity

The artifact is a **CLAUDE.md** (or **CLAUDE.local.md**): a file Claude Code loads ambient for any session whose cwd or file access sits at or beneath its directory. Its canonical structured payload is the `claude_md:` YAML block (scope + insights + optional conventions / glossary), validated by `skills_kit_lib.audit` against `CLAUDE_MD_SCHEMA`.

**Load-trigger note (reachability).** A non-root CLAUDE.md loads on BOTH triggers: cwd descent into its directory AND file access (Read/Edit/Write) beneath it. Data directories and leaf code packages are typically worked by path from a repo-root cwd -- their CLAUDE.md files (review rails for committed-data diffs, package review notes) are REACHABLE and correctly placed. Do not flag such a file as dead weight or its facts for bubbling to root on a cwd-only reachability model; the reader set is "sessions touching files beneath this directory", by cwd or by path (see cohesion-principles `directory_claude_md`).

### 1.1 Role (which subset of the classic standards applies)

The role of a CLAUDE.md determines which subset of criteria applies. Roles are computed relative to the current working directory.

| Role | Definition | Criteria applied |
|---|---|---|
| `root` | CLAUDE.md at cwd when no CLAUDE.md exists above it -- claude was launched at the project top | CCP (all), CRP (all), ADP (all), Hygiene (all incl. H-1/H-2/H-3) |
| `child` | CLAUDE.md below cwd, OR at cwd when an ancestor CLAUDE.md exists above it (directory-local / subordinate scope) | CCP (incl. parent-child), CRP, ADP, Hygiene (skip H-1/H-2/H-3 -- those belong to root only) |
| `ancestor` | CLAUDE.md above cwd (loaded ambient from the user's session) | CCP (all), CRP (all), ADP (all), Hygiene (all) |
| `local` | CLAUDE.local.md at any directory | CCP only (C-3, C-4); ADP and Hygiene skipped because the file is personal-scoped by design |

### 1.2 Dimension: classic vs code-directory (orthogonal to role)

Independently of role, the discover script flags each file `dimension: code-directory | classic` (the Level-1 trigger; see `../../scripts/discover_claude_md.py::classify_dimension`).

- A **code-directory** file -- a per-directory review-notes CLAUDE.md sitting inside code/YAML/CSV, carrying gotchas / Review Checks / boundary claims and **no** `claude_md:` block -- gets the classic standards **plus** section 3 (CD-1..CD-6: anchor-modality classification, fidelity, value filter).
- A **classic** file -- root project file, docs, skill dir, or any file declaring a `claude_md:` block -- runs the classic standards only. Section 3 does not apply to it.

Authoring detection mirrors the audit's Level-1 trigger: code/data siblings, or review-claim / shape markers, and no `claude_md:` block / no sibling SKILL.md.

### 1.3 Lens: density (opt-in)

Section 4 (DD-1..DD-4) is an **opt-in** lens that fires in addition to the classic standards only when the run requests it (the `density` argument, or prose intent like "is this CLAUDE.md too verbose / can anything move to a reference"). For a normal run none of it applies and default behavior is byte-for-byte unchanged. The lens is **advisory only**: every criterion is JUDGMENT severity, every taxonomy row is DISCUSS/IMPROVE, and it **never emits FAIL and never emits AUTO**.

### 1.4 Compliance semantics

- Any FAIL finding -> file is NON-COMPLIANT.
- Only PASS and INFO findings -> file is COMPLIANT.
- INFO findings are advisory improvements, not compliance failures.
- INFO findings do not escalate to FAIL on subsequent runs even if unaddressed.

**What COMPLIANT asserts, and what it does not.** COMPLIANT means: *no FAIL
under the document criteria listed in this file*. It is a statement about the
document as an artifact -- its schema, its internal cohesion, and the truth of
the assertions it makes. It is NOT a statement that the document is sufficient
for any downstream task, and specifically **not** that a reviewer working from
it would catch a defective change: no criterion here evaluates whether the
directory's real hazard surface is covered, because the audit validates existing
claims and does not crawl for absent ones (section 3, "not a gotcha crawler").
A thin file that says little, accurately, is COMPLIANT.

Report accordingly: a COMPLIANT verdict is reported as *no document-criteria
failures*, with coverage explicitly noted as not assessed. This mirrors the
weaker-and-honest posture review mode already takes with `DIFF-CLEAN` (see the
audit lane's "Review mode"). Do not present COMPLIANT as an endorsement of the
document's usefulness.
- Idempotency: same file + same tree -> same findings, same verdict. Criteria, taxonomy, and bucket assignments are fixed; do not re-rank or re-order session-to-session.

---

## 2. Classic standards

Organized by cohesion principle (CCP / CRP / ADP) plus universal hygiene. Each rule has a testable statement and a severity (FAIL / INFO / PASS).

### 2.1 CCP standards (write-together / change cadence)

CCP says: content that changes for the same reason belongs in the same file. A CLAUDE.md fact lives where its change driver lives.

#### C-1. Parent-child duplication

**Rule:** A child CLAUDE.md must not repeat instructions present in any ancestor CLAUDE.md.

**Why CCP:** the duplicated facts share the same change driver (whatever caused the parent to state the rule causes the child to need updating); SSOT is broken; updates drift between copies.

**Test:** read parent CLAUDE.md content; for each instruction in the child, check whether the same instruction (verbatim or near-verbatim) is in any ancestor. The test is TEXTUAL: verbatim or near-verbatim sentence-level similarity of the instruction/content. An argument that the child "operationalizes" the ancestor's concepts (e.g. as review rails), serves a different reading task, or has a different change driver does NOT exempt a near-verbatim restatement -- if the sentences match, the finding fires regardless of the surrounding framing.

**Severity:** FAIL (each duplicated instruction is a finding).

**Remedy:** delete the duplicate from the child; the ancestor instruction already loads ambient. Exception: a one-line guardrail naming the rule or failure mode may remain per C-5's "never flag instructions that document known agent failure modes" and A-4's "keep a one-line guardrail in CLAUDE.md naming the error; the skill carries the depth" -- multi-sentence restatement of mechanics/detail still fails, so the remedy for such lines is trim-to-guardrail-plus-pointer, not deletion.

#### C-2. Sibling duplication

**Rule:** When the same instruction appears in multiple sibling CLAUDE.md files, it belongs in their common ancestor.

**Why CCP:** sibling duplication is a CCP signal that the change driver is shared; a single ancestor placement is the SSOT remedy.

**Test:** when auditing multiple files, scan for instructions present in 2+ sibling files. Flag instructions that share the same parent.

**Severity:** FAIL (instruction duplicated across siblings). **Remedy:** move the instruction to the common ancestor; remove from siblings.

#### C-3. Personal-vs-shared cadence (CLAUDE.local.md only)

**Rule:** CLAUDE.local.md contains only machine-specific paths, personal preferences, and individual overrides. Team-useful content belongs in the shared CLAUDE.md.

**Why CCP:** personal preferences change at a different cadence than team conventions; mixing them forces shared-file edits when only personal preferences shift.

**Test:** for each instruction in the .local file, ask "would another team member benefit from this fact?" If yes, flag.

**Severity:** FAIL on team-useful content in .local. **Remedy:** move team-useful content to the shared CLAUDE.md.

#### C-4. Local duplication of shared (CLAUDE.local.md only)

**Rule:** CLAUDE.local.md must not repeat instructions from the shared CLAUDE.md.

**Why CCP:** same SSOT violation as C-1, applied to the local-vs-shared pair.

**Test:** diff against shared CLAUDE.md. **Severity:** FAIL on duplications.

#### C-5. Content earns its place

**Rule:** Each instruction passes the test "Would removing this cause the agent to make a mistake?" If not, it is a non-load-bearing fact and should be removed or moved to a deferred reference.

**Why CCP:** a fact with no change driver in this scope's directory has no reason to live here; nothing local would cause it to update. Same-cadence content with no driver is decay.

**Test:** for each instruction, ask: (1) Would removing this cause the agent to make a mistake in a typical session in this scope? (2) Is the fact already injected by the system (e.g. skill names auto-listed), making the local copy redundant?

**Critical exception:** common agent error patterns (things the agent repeatedly gets wrong) MUST stay in CLAUDE.md even if they could theoretically live in a skill. Skill invocation is not reliable enough to gate error-prone behaviors behind. Never flag instructions that document known agent failure modes.

**Severity:** FAIL on non-load-bearing instructions outside the exception.

#### C-6. Project-reference duplication of skill content

**Rule:** A CLAUDE.md must not embed (or cite a project reference doc that embeds) content that already lives in a skill.

**Why CCP/SSOT:** when a skill exists for a topic, the skill's references/ folder is the SSOT. A CLAUDE.md that duplicates skill content (inline or via a parallel project reference doc) creates two copies that drift independently.

**Test:** for each substantial block of CLAUDE.md content (or each project-reference doc cited from CLAUDE.md), check whether a skill exists for the same topic. If yes, the content should collapse to a pointer (`for X, invoke /example:skill-name`) or, where the harness supports it, a `required-skills:` declaration. The test is TEXTUAL: verbatim or near-verbatim similarity of the instruction/content against the skill's own text. An argument that the CLAUDE.md block "operationalizes" the skill content for a different reading task or change driver does NOT exempt a near-verbatim restatement -- if the sentences match, the finding fires.

**Severity:** FAIL on duplicated skill content. INFO when the project-reference predates the skill and graduation work is in progress. Exception: a one-line guardrail naming the rule or failure mode may remain per C-5's "never flag instructions that document known agent failure modes" and A-4's "keep a one-line guardrail in CLAUDE.md naming the error; the skill carries the depth" -- multi-sentence restatement of mechanics/detail still fails, so the remedy for such lines is trim-to-guardrail-plus-pointer, not deletion.

#### C-7. No field-by-field restatement of a code-enforced contract

**Rule:** For a machine contract that code validates (a schema enforced by a validator module, a wire format enforced by a parser), the validating code's in-code doc (module docstring / contract comment) is the SSOT for the field-level detail. A CLAUDE.md states the contract's existence, its invariants, and its change-discipline ("the schema changes in the same diff as the validator"), and cites the module for the fields. It must not enumerate the schema field-by-field.

**Why CCP/SSOT:** the in-code doc changes in the same diff as the validator (perfect CCP); an md copy of the field list changes in a different diff and drifts while still looking authoritative.

**Test:** for each schema/format the file describes, check whether code validates it; if yes, check whether the file restates the field list rather than citing the module.

**Severity:** FAIL on live field-by-field restatement; INFO when the restatement is explicitly flagged in-doc as an accepted drift risk.

### 2.2 CRP standards (read-together / smallest reader-set)

CRP says: a fact lives in the smallest scope whose readers all need it. Readers of this scope should plausibly need every fact in the scope.

#### R-1. Directory-appropriate content

**Rule:** Content only relevant when working in a specific subdirectory should live in that subdirectory's CLAUDE.md, not at a higher scope.

**Why CRP:** a fact that does not fire for a typical reader of this scope should bubble down to the scope where every reader needs it.

**Test:** for each instruction, ask "would an agent working in a sibling part of this scope need this?" If no, the fact belongs closer to the code it describes.

**Severity:** INFO (migration opportunity; not always wrong -- poor directory organization sometimes requires explanations at a higher level).

#### R-2. Self-contained context (CRP within file)

**Rule:** Each CLAUDE.md must be understandable without reading any document other than its ancestor CLAUDE.md files (which are always loaded). Project-specific terminology (system names, API names, acronyms) must be established before reference.

**Why CRP:** sections within the file form their own internal load order. A reader hitting a project-specific term must have already encountered its identity; otherwise the reader cannot use the surrounding instructions.

**Test:** scan for project-specific terms; for each, verify a one-line identity establishment occurs at first use (in this file or an ancestor).

**Severity:** FAIL on terms used without prior identity. **Remedy:** add a one-line identity ("X is the Y system") at first use.

#### R-3. Size signals (CRP evaluation prompt, not verdict)

**Rule:** Files that exceed 500 lines / 3000 tokens deserve a CRP evaluation, not an automatic split.

**Why CRP:** the threshold is a signal that the file may have accumulated multiple reading tasks. Splitting is correct ONLY if a CRP-passing decomposition exists -- sections must serve different reading tasks. A stub-with-always-co-loaded reference is CRP-fail (tool-call doubling without context-efficiency win); see `../cohesion-principles.md` and the skill standards' "CRP is the test for L2 -> L3 splits."

**Test:** (1) Count effective lines (excluding trailing blanks). (2) If > 200 (root) or > 60 (child) ideal: emit INFO finding. (3) If > 500 lines or > 3000 tokens: emit INFO recommending a CRP evaluation, with explicit warning that splitting is only legitimate when sections serve different reading tasks.

**Severity:** INFO at all sizes (size alone is never FAIL).

#### R-4. Progressive-disclosure opportunities

**Rule:** When a file exceeds size ideals, identify content that could legitimately move to one of four destinations, in order of preference:

1. **A skill** (SKILL.md + structured contract) -- if the content fits a skill type (procedure -> technique-skill; rule + counter -> discipline-skill; lookup table -> reference-skill; tool/MCP/API wrapper -> capability-skill). This is the highest-leverage destination: discoverable trigger, audit surface, typed contract.
2. **A skill's references/ folder** -- if the content already belongs to an existing skill but lives inline in CLAUDE.md by accident. Cite via `for X, invoke /example:skill-name`.
3. **A project reference doc** (a markdown file outside any skill, e.g. `<project>/docs/<topic>.md` or `.claude/docs/<topic>.md`) -- the escape hatch when the content does not yet fit a skill type but is too large for inline. Useful for emerging concepts that may eventually graduate into a skill (see "Skill-maturation pipeline" in `../cohesion-principles.md`).
4. **A child CLAUDE.md** (loaded lazily when agent enters that directory) -- if the content is directory-specific and serves the in-directory editor reader.

**Maturation flag:** when identifying a project-reference destination, also check whether the content has matured into a structured shape that fits a skill type. If yes, recommend graduation into a skill rather than placement as a project reference.

**Critical exclusions** (never flag for migration): common agent error patterns (must stay inline; gating behind a skill is C-5 / A-4 territory); build commands (needed on nearly every session); gotchas / traps (highest-value content; keep prominent).

**Severity:** INFO (each migration candidate is a separate finding).

### 2.3 ADP standards (link-forward-only / DAG)

ADP says: file references run downward in load order. Each file may cite earlier-loaded files; later-loaded files must not be cited as load dependencies.

**Legitimate forward edges from a CLAUDE.md** (not flagged by these rules):

- CLAUDE.md -> project reference doc, e.g. `for migration patterns, see docs/migration-guide.md when working on database changes`. The pointer is informational; CLAUDE.md instructions remain complete without the reference being loaded.
- CLAUDE.md -> skill via prose pointer, e.g. `for any Python work, invoke /python-coding`. The pointer names a downstream skill the agent should invoke.
- CLAUDE.md -> skill via YAML header, e.g. `required-skills: [python-coding]`. Where the harness supports it, this declares a skill that should be auto-loaded when the CLAUDE.md is in scope. Confirm harness support before relying on the field.

The criteria below flag the prohibited cases.

#### A-1. Referenced documents exist

**Rule:** Every cross-file reference (`see X`, `refer to X`, `documented in X`, paths in instructions) must resolve to a file that exists.

**Why ADP:** a broken edge breaks the DAG; the agent has no path to the cited content.

**Test:** for each path-like reference, use Glob or filesystem check to verify existence. **Severity:** FAIL on missing references.

#### A-2. No `@import` of large content

**Rule:** `@import` (lines starting with `@path/to/file`) inlines the imported file at session start -- it is NOT lazy. Files over ~50 lines imported this way should use deferred references instead.

**Why ADP:** `@import` collapses the load graph by inlining; large imports inflate L1 with content that should be at L2 or L3.

**Test:** scan for lines matching `^@`; for each, check the imported file's size; flag imports of files > 50 lines.

**Severity:** FAIL on large `@import`. **Remedy:** replace `@path/to/file` with prose: "See path/to/file when working on X."

#### A-3. Stale references

**Rule:** References to files, commands, sections, or patterns that no longer exist break the DAG.

**Why ADP:** broken edges. Includes file paths (overlap with A-1) and non-file references like CLI flags, class names, internal section headers.

**Test:** check internal section-header references against the file's actual headers; check CLI flag references against the project's tooling; check class-name references against grep results.

**Severity:** FAIL on stale references.

**Count claims -- exact-enumeration vs illustrative-magnitude (settled 2026-08-07).** A count-shaped claim is one of two kinds, and only one of them is checkable:

- **Exact-enumeration** -- the number is what a reader relies on to know the list is COMPLETE. Tells: a definite article plus a plural noun naming a closed set ("the eleven native systems", "the six unittest suites"), a count the reader would use to decide nothing is missing, or a count the doc pairs with its own enumeration. These are contractual and MUST be verified.
- **Illustrative-magnitude** -- the number conveys SCALE and the claim survives the number drifting ("a 7200-line god object", "~40 anchors", "resets 10 fields"). The *kind* is the claim; the number is color. These are NOT verified and never produce a finding on the number (section 3.4, CD-4).

When the kind is genuinely ambiguous, treat it as illustrative -- a false count finding costs more than a missed one.

**Verify by ENUMERATING, never against adjacent prose.** Check an exact-enumeration claim by counting the actual registrations, definitions, or entries (grep the registration call and count the call sites; list the test definitions; list the directory). Do NOT satisfy the check by reading the nearest comment, docstring, or heading that restates the number. A stale figure is usually echoed in the source's own comment, so **a fact-check performed against adjacent prose CONFIRMS a wrong doc** -- the executable content is the ground truth, and human-readable text beside it is another copy of the claim, not evidence for it. (Worked case: a doc said "the eleven native systems" while the registration site held 19; the source's own comment above that site also said eleven.)

**The enumeration must be CLOSED and mechanically identifiable.** A count is verifiable only when the set has a definite boundary you can point at: an enum body, a registration list, a directory listing, a test roster. If membership is decided by template / `if constexpr` / attribute / reflection dispatch, by code generation, or by a governing flag with no live consumer, the count is **UNVERIFIABLE -- suppress it; do not estimate**. Grepping a dispatched family returns call sites, not the requirement set, and a number derived that way is a guess wearing a verification's clothes. (Measured: on a held-out C# / C++ corpus this was the single false-positive source in the enumeration half.)

**Units must be unambiguous, and ambiguity favours the doc.** If the counted noun admits more than one defensible unit -- files vs headers vs translation units vs types; bullets vs distinct tools -- and **any** one of those units makes the stated count correct, suppress. Only fire when the count is wrong under every reasonable reading. Where the real defect is coverage rather than arithmetic ("three of these are undocumented"), say that instead of disputing the number.

**Enumerate TRANSITIVELY, and show the hops.** Registrations are routinely split across helpers, partial classes, generated files, or attribute/reflection-based registries, so a count taken at one call site is usually a floor rather than the total. Follow the registration helpers a site calls, and count the whole reachable set. State the hops in the finding ("12 at `mb_register_systems` + 7 via `mb_campaign_register_systems` = 19"). This is not optional bookkeeping: the disposition is **FIX and the correction is applied**, so an enumeration stopped one hop short writes a NEW wrong number into the doc -- strictly worse than the stale one, because it now carries a fresh verification. If the reachable set cannot be enumerated with confidence (reflection, attribute scanning, code generation, a registry assembled at runtime), do NOT emit FIX: downgrade to IMPROVE, state the number you could derive, and name what you could not reach.

**Scope (extended 2026-08-07):** `P_stale_factual_claim` fires on a **classic** file for any checkable factual claim, and on a **code-directory** file for **exact-enumeration count claims only**. Nothing else in section 2 extends to code-directory files, and the illustrative-magnitude posture is unchanged -- see the narrowing notes at section 3.4 and CD-4. On a code-directory file the finding is severity JUDGMENT (only CD-2 gates that dimension) with disposition **FIX**: recount and correct the number, information-preservingly (correct the count AND add the missing entry). Provenance: `../provenance/standards-decisions.md`, `count_typing_exact_vs_illustrative`.

**Provenance citations to ephemeral artifacts (settled 2026-08-03):** an `origin:` or other provenance field may cite ephemeral or session-scratch work by description, date, and finding ids -- never by path, unless the path is tracked by version control. A path-form citation to a gitignored or otherwise untracked location (e.g. `tmp/...`) is stale the moment it is written: it resolves for no reader except the authoring session, and inviting a chase to a path that cannot exist is the same broken edge A-3 exists to prevent. Severity: FIX, always loss-free -- drop the path, keep the description, date, and finding ids. A tracked, resolvable path in a provenance field is fine and is checked like any other reference. This rule is decisive: do not classify such a citation as an accepted historical pattern.

#### A-4. Skills gating common errors

**Rule:** A common agent error pattern must be reachable from CLAUDE.md (always-loaded layer), not gated solely behind a skill's trigger.

**Why ADP:** skill invocation is conditional on the description matching the user's request; common errors fire in many contexts that may not trigger the skill. Gating a common error solely behind a skill creates a load-graph dependency on a trigger that may not fire.

**Test:** scan for `for X, see /Y` style pointers; for each, judge whether the underlying fact is a common agent error. If it is, the fact (or at least a one-line guardrail) must be inline in CLAUDE.md.

**Severity:** FAIL on common-error gating. **Remedy:** keep a one-line guardrail in CLAUDE.md naming the error; the skill carries the depth.

#### A-5. Parent-to-child citation as load dependency

**Rule:** A parent CLAUDE.md must not cite a child CLAUDE.md by name as required content.

**Why ADP:** the child loads conditionally on cwd; sessions where the child does not load see an incomplete parent. The parent's correctness must not depend on a downstream load.

**Permitted:** the parent may say "for X-specific work, see <X>/CLAUDE.md" as an orientation pointer, IF the parent's instructions are complete without the child being loaded.

**Test:** scan for `see <subdir>/CLAUDE.md`-style references; for each, check whether the parent's instruction is incomplete without the child.

**Severity:** FAIL on incomplete-without-child references.

### 2.4 Hygiene standards (universal)

Not derived from cohesion principles -- agent-fluency rules retained because the failure modes are common and the remedies are mechanical.

#### H-1. Project identity (root only)

Root CLAUDE.md includes a brief project description (what it is, tech stack). 1-3 lines. Severity: FAIL if missing.

#### H-2. Essential commands (root only)

Root CLAUDE.md presents build / test / lint commands as exact runnable commands, not prose. Severity: FAIL if missing for a project that has builds/tests.

#### H-3. Directory structure (root only)

Root CLAUDE.md includes a high-level directory map showing where major components live. Severity: FAIL if missing for a multi-component project.

#### H-4. Prohibitions have positive alternatives

Every prohibition ("never use X", "don't do X") includes a positive alternative ("instead use Y", "prefer Z"). An agent with no path forward will ignore the rule or get stuck.

Test: scan for negation patterns; verify each has a corresponding "instead", "prefer", or "use Y".

Severity: FAIL on prohibitions without alternatives, with the documented exception of cases where there is no safe alternative (e.g. "never `p4 obliterate` -- destroys history" needs no alternative).

#### H-5. No personality instructions

No "be a senior engineer", "act as an expert", "you are a helpful assistant" or similar. The model already reasons at expert level; these waste tokens. Severity: FAIL on personality directives.

#### H-6. No generic programming advice

No instructions the model would follow without being told (e.g. "write clean code", "use meaningful variable names", "handle errors appropriately"). Test: would Claude do this anyway without the instruction? If yes, FAIL. Severity: FAIL on generic advice.

#### H-7. Gotchas are specific and actionable

Gotcha entries describe a concrete failure mode AND how to avoid it. Vague warnings ("be careful with X", "watch out for Y") fail this criterion. Severity: FAIL on vague gotchas.

#### H-8. No linter-enforced style rules

Style rules that a linter / formatter already enforces are noise, unless documenting a common agent error the linter cannot catch. Severity: FAIL on linter-redundant style rules.

#### H-9. No embedded documentation

Exhaustive API docs, architecture deep-dives, or long reference tables that should be deferred references.

Boundary -- deferred-reference pointer lists: a list of pointers to deferred references is not embedded documentation. The annotation ceiling is one "read when ..." line per pointer, with exactly one exception (settled 2026-08-03): an annotation may exceed the ceiling ONLY where the extra lines state a **constraint or agent-error driver not stated at the target** (e.g. "schema_registry.py wins on divergence", "refuses to overwrite without --force"). Extra lines that summarize the TARGET'S OWN content or structure -- its section list, its file-by-file layout, its config format, its precedence chain -- re-embed the documentation the pointer exists to defer and trip this criterion regardless of the map's routing value; the routing value lives in the pointer plus its one line, not in the recap. "This map is load-bearing for routing" is not an exemption: apply the per-annotation test line by line, keep the error-driver lines, collapse the recap lines.

Severity: FAIL on embedded documentation > 30 lines that has no agent-error driver.

#### H-10. No unpruned auto-generation

Signs of `/init` output never edited: boilerplate headers ("This file provides guidance..."), placeholder sections, obvious filler. Severity: FAIL on visible auto-generation artifacts.

#### H-11. Obeys ancestor-declared conventions

An audited file must not violate a convention that an **ancestor CLAUDE.md explicitly declares**. Ancestor CLAUDE.md files -- every CLAUDE.md above the subject on the directory path up to the workspace root -- load ambient in any session that touches the subject, so their stated conventions bind the subject exactly as they bind any file in their scope. Typical declared conventions: ASCII-only mandates ("never write non-ASCII characters into source files"), "no absolute paths in shared files", required formatting or structure rules the project's CLAUDE.md states.

**Rule-extraction posture (mirrors the code-review reviewer_a):** a violation may be flagged ONLY when the exact declared rule can be **quoted verbatim** from an ancestor CLAUDE.md. No inferred conventions, no generic best-practice, no "the spirit of" a rule, no convention you believe is standard but the ancestor did not write down. If you cannot quote the ancestor's rule text verbatim, do not raise the finding.

**Anchor + message:** the finding anchors on the SUBJECT file (the line that violates the rule), and its message carries (a) the **verbatim ancestor rule quote** and (b) the **source path** of the ancestor CLAUDE.md that declared it -- so the author can see both what was violated and where the rule lives.

**Scope:** fires only when ancestor CLAUDE.md files are supplied to the audit (the `ancestorClaudeMdPaths` argument, nearest-ancestor first). A `root`-role file with no ancestors, or a run that supplies no ancestor paths, emits no H-11 findings.

**Ancestor-declared exceptions suppress the built-in universal conventions.** The classifier also carries hardcoded universal-convention checks (a non-ASCII look-alike or a hardcoded absolute path is a convention-violation FIX unconditionally). Those are made **exception-aware** by the same ancestor CLAUDE.md declarations H-11 reads: when an ancestor **explicitly declares a scoped exception** that covers the specific instance -- the right file scope AND the right content kind, e.g. *"ASCII only, except developer names in the contributors section may contain non-ASCII characters"* -- the built-in check does NOT emit the FIX; it demotes to PASS/INFO citing the verbatim exception quote + ancestor source path. The exception must be written down and actually cover the instance (same verbatim posture as H-11; no inferred or stretched exceptions, and when in doubt the built-in check still fires). Precedence is deliberate: H-11 and the built-in check read the *same* declared rule + exception, so they must yield one consistent outcome -- an exception that silences H-11 silences the built-in FIX too, and vice versa. This is the contradiction the exception-awareness removes (H-11 silent while the built-in check fires on the same instance). When no ancestor paths are supplied, or no exception is declared, the built-in checks behave exactly as before.

Taxonomy: `R_ancestor_convention_violation` (group Hygiene). Disposition is assigned instance-level by the classifier like any other convention-violation fix -- normally **FIX** (a mechanical correction against a documented project convention: replace a non-ASCII look-alike, relativize a hardcoded absolute path, apply the stated formatting rule), and **SERIOUS** when the violation reveals a real-world problem the ancestor's rule exists to prevent (e.g. a committed secret an ancestor forbids).

Severity: FAIL on a verbatim-quotable ancestor-convention violation.

---

## 3. Code-directory dimension

**One standard, two directions.** A code-directory CLAUDE.md is a per-directory review-notes file inside (or describing) a directory of code, YAML, or CSV. Sections 3.1 to 3.5 state what such a file must look like; section 3.6 states how the audit direction validates it (CD-1..CD-6) and section 3.7 how the authoring direction produces it. Both directions read the same shapes, observation kinds, idioms, anchoring discipline, and value filter below -- authoring to this standard is exactly what keeps the audit green.

These files are not librarian artifacts -- they are **distilled review intelligence about one directory**. Their failure mode is not misplacement; it is **the claims rotted** (the god-object got decomposed, the sibling config was renamed, the line anchor drifted) or **the insight stopped earning its place**.

**The north star.** The file will be read by a code-review agent reviewing a diff in this directory. **A section earns its place only if it makes that agent catch something a senior teammate catches and a generic reviewer misses.** You are an experienced tech lead writing the onboarding notes you wish you'd had -- not documentation. Every section costs attention budget; spend it on what goes wrong, not on what's obvious.

Placement (which CLAUDE.md a fact belongs in) still defers to `../cohesion-principles.md`. A code-directory file carries **no** `claude_md:` block and is **not** run through the schema validator.

**The dimension does not waive the role hygiene rules.** The role table in
section 1 governs regardless of dimension: a `root`-role file is checked
against H-1/H-2/H-3 even when its dimension is code-directory. Shape B's
"one-line purpose" is a body shape, not a substitute for H-1 -- a root-role
review-notes file with no project identity (what this is, what it is built
with) still FAILs H-1. (Audit-parity rule: the pre-fold claude-md audit
applied H-1 to root-role code-directory files, and the golden corpus locks
that behavior; the generation shapes folded in from the generation direction
describe how to write the body, not which hygiene rules apply.)

### 3.1 The four shapes (mixing is allowed)

A file may legitimately **mix** shapes (an architecture preamble + a gotcha list + a cross-child rule block in one file). Identify the dominant shape and note any mixed-in ones; do not flag a file for mixing, and do not split a cohesive file just to keep one shape. Identifying the shape gates nothing -- it only tells you which observation kinds to expect.

- **Shape A -- gotcha-per-section** (source code: C++, C#, Python). A `##` heading is usually a claim; the body gives an anchor + a why + a do-instead.
- **Shape B -- purpose + Schema/Files + Review Checks** (data/config: YAML, CSV). One-line purpose, an *annotated* structural section, then a `## Review Checks` section -- that section is the payload, not the prose. An *annotated* `## Files`/`## Schema` block (each entry carries a constraint) is payload, not inventory.
- **Shape C -- boundary / ownership** (directory-level; common in infra). Headings are boundary statements; body says what lives here vs. not, plus safety rails and ordering invariants. Often a `## Children` index with cross-cutting blast-radius notes.
- **Shape D -- architecture / pointer-hub exposition**. Headings are **labels, not claims**; a descriptive lead paragraph is fine. Anchors are **named patterns** or **pointers to an SSOT**, not symbol/line pointers. Attach the do-instead to the specific invariant ("do not diverge from the binary layout"), not the topic heading. Pointer-hub files mostly say "this rule is universal; payload lives in `<SSOT>`; here is the one local delta." Do NOT try to resolve a symbol/line anchor for a Shape-D heading -- validate that its pointer target (the doc it points to) resolves.

### 3.2 High-value observation kinds, per shape

The generation direction asks which of these are present **and silent** in this directory, writes those, and skips the rest. The audit direction uses the same list to recognize value -- do not go hunting for missing kinds.

- **Shape A:** god-object/don't-add-here - deliberate hack/workaround (and *don't simplify*) - diff-invisible perf trap (O(N), per-tick) - lifetime/ownership hazard (raw `this` capture, must-outlive) - type-safety bypass (*don't copy*) - dead/misleading code - build-flag-dependent behavior - lifecycle-method contract (what goes in which method) - "use the helper, not inline".
- **Shape B:** cross-config referential integrity, naming the **silent-failure mode** (*"mismatches are silent at build time"* -- highest value) - rename/removal blast radius (*"search for usages first"*) - "not just config review" escalation - secrets hygiene - asset/external-path validity - **append-only data-ledger** (order-immutable, sentinel value, "removing an entry corrupts save data").
- **Shape C:** allowed/FORBIDDEN safety rails - gitignored-by-design / tracked-file-is-a-leak - vendored subtree ("review provenance not bytes") - deploy/migration ordering invariant - children-index blast radius - pointer to a universal rule (don't restate).
- **Shape D:** architecture exposition (named-pattern invariant: "mirrors the binary schema -- do not diverge") - pointer-hub ("rule is universal; payload in `<SSOT>`; here is the one local delta") - **external-contract** (local constant + "the other side lives in `<repo>`; verify by hand" -- a *complete* claim, not a defective bare prohibition).

### 3.3 Two idioms the generic "anchor + why + do-instead" rule doesn't cover

- **External-contract:** when the other side of a contract lives in another repo (a C++ client consuming this REST API; wire constants that must match the gameserver), the complete claim is **the local constant + "the other side lives in `<repo>`; verify by hand."** This is finished, not a defective bare prohibition -- don't force a do-instead onto it.
- **Negative-existence (assert-absence):** for secrets dirs and forbidden targets, the claim shape is **assert-absence + the detection-trigger**: "this is gitignored / does not exist in a clean checkout; **a tracked file here is the finding.**" The "do-instead" is the detection rule itself.

### 3.4 Anchoring and path discipline (this is what makes claims survive)

- **Prefer a symbol anchor over a line anchor.** Line numbers rot fast (in our corpus, 3 of 4 sampled line anchors were already 5-190 lines off). **Drop the line number entirely unless the gotcha is sub-function**; when a line genuinely helps, mark it best-effort (`~2204-2215`) *and* name the enclosing symbol so it's recoverable after drift. If you must cite a volatile line, give the recovery hint ("run `grep -n '# NOTE:'` first").
- **Counted magnitudes are illustrative, not contractual** -- **for illustrative-magnitude counts only (narrowed 2026-08-07).** "7200-line god object" communicates the kind; don't sweat the exact number -- but write the claim so it stays true even as the number drifts (the *kind* is the claim, the number is color). This licence does **not** cover an **exact-enumeration** count -- a number the reader relies on as a complete list ("the eleven native systems", "the six unittest suites"). Those are contractual: verify them by enumerating the code, never against an adjacent comment that restates the number, and a wrong one is a `P_stale_factual_claim` FIX. Definition, tells, and the enumerate-don't-read-the-comment discipline: A-3's count-claims rule (section 2.3).
- **Every prohibitive claim states the why and a do-instead** ("don't add to it -> put new behavior in a UActorComponent"). A bare prohibition is a defect -- *except* the external-contract and negative-existence idioms above, which are complete as written.
- **Near-sibling references -> relative** (`../Cohorts/`, `../BuildingTileset/file.yaml`).
- **Cross-subtree universal-rule pointers -> repo-root-absolute with a leading slash** (`/docs/code-review/...`, `/kubernetes/CLAUDE.md`). This is more robust than fragile `../../../` chains and is a deliberate, good convention.
- **Never** tree-absolute *without* a leading slash (`GameConfigs/Real/Items/`) -- that is the single most common broken-reference pattern in our corpus; it resolves from nowhere.
- Resolve symbol anchors **repo-wide**, not directory-locally -- a YAML dir legitimately cites a `.cs` symbol in another module (e.g. `MigratedIds.cs`). A leading-slash path (`/docs/...`, `/kubernetes/CLAUDE.md`) resolves against **repo root**, not filesystem root.

### 3.5 The value filter ("what we care about")

An insight earns its place only if a code-review agent that read it would catch something a senior teammate catches and a generic reviewer misses. Rank kept content by:

1. **Silent failure** -- no compiler/linter/type-checker/test/CI catches it. *Highest value.*
2. **Blast radius / coupling** -- a change here breaks something *there*, across a file/dir/repo boundary.
3. **Deliberately-wrong-looking** -- looks like it should be "fixed/simplified" but must not be.
4. **Safety / security rails** -- forbidden targets, secrets hygiene, auth boundaries.
5. **Diff-invisible performance** -- per-tick spam, O(N) loops that read as O(1) in the hunk.
6. **Ownership / boundary** -- what belongs here; what to review vs. ignore (vendored).

**Low-value (taxonomy J) -- do not write it; flag it if present:** linter/compiler/CI-enforced rules; language/framework defaults; generic programming advice; a rule already stated in an ancestor CLAUDE.md (CCP duplication -- defer to the classic CCP criteria); a **bare** directory inventory or file listing for its own sake; an empty heading with no claim; a self-describing schema restated as the file's substance.

**Carve-outs -- do NOT flag these as low-value (both maintainer-agents required them):**

- An **annotated** `## Files`/`## Schema` block whose entries each carry a constraint (`credentials.json` must be a template; `cohortConfigId` must match `Cohorts/`) -- it is payload; the Review Checks depend on it. Never AUTO-delete.
- A denormalized **constraint catalog that points to its SSOT** (e.g. an EKS C1-F3 list mirroring a runbook) -- a navigational cheatsheet by design.
- **Operational cheatsheets that scope a safety rail** (the allowed kubectl/deploy commands next to a FORBIDDEN list) -- part of the rail.
- **Topology / ownership tables** (cluster->namespace->deployments, `KNOWN_ACCOUNTS`, account-id duality) -- blast-radius coupling maps, not inventories.

**Named anti-patterns (generation negative space):** no bare directory inventories - no empty CLAUDE.md (a heading with no claim) - no schema restatement as substance - no line-only anchors lacking a symbol - no tree-absolute-without-leading-slash sibling paths - no language defaults or linter-enforced style - no do-instead-less prohibition (except the external-contract / negative-existence idioms).

### 3.6 Audit direction: the CD criteria

The dimension is a **validator over existing claims, not a gotcha crawler** -- it does not scan the directory for *new* gotchas to add (that is the generation direction; doing it here would be non-idempotent and expensive).

**Two-level recognition model.** Level 1 (the discover script) decided *whether* this dimension runs (the file is flagged `code-directory`). Level 2 (below) decides *how hard* to scrutinize each claim -- via the anchor-modality classifier. Only one anchor modality is ever eligible for FAIL. This is the safety valve: because Level 1 triggers generously, Level 2 must be strict about what can FAIL, so accurate negative-existence / external / templated / generated claims are never punished.

**Anchor modality table.** For each concrete anchor a claim makes (a symbol, file, sibling path, field, name, command), tag exactly one modality. **Only `requires-present` is eligible for a FAIL.** This classification is the gate; run it first.

| Modality | How to recognize it | Scoring |
|---|---|---|
| **requires-present** | a symbol/file/sibling the claim says *should exist* (`TryAction`, `../Cohorts/`, a field name) | the ONLY modality eligible for FAIL when named-and-absent |
| **requires-absent** (negative-existence) | the claim asserts/requires absence: "gitignored", "does not exist in a clean checkout", "a tracked X here is a leak", a FORBIDDEN list | **inverted**: absence = PASS; *presence* of the asserted-absent thing = FAIL (taxonomy H2) |
| **external-unverifiable** | lives outside this repo/VCS: cross-repo Perforce `//depot/...`, cluster-side context/namespace/AWS-profile names, 1Password / Secrets-Manager refs, another repo's HTTP contract | INFO / UNVERIFIABLE, **never FAIL** |
| **template-or-env** | `{{ .Values.* }}`, `$DB_PASSWORD`, `secretKeyRef` targets, helm/k8s runtime names | resolve against the template/values graph if cheap, else UNVERIFIABLE; **never grep as a literal, never FAIL** |
| **vendored-don't-read** | a vendored binary/subtree (`aws-iam-authenticator`) | confirm presence only; **never open the bytes** |
| **generated-or-unsynced** | matches `*Generated.*`, lives under `Intermediate/`/`Saved/`/`node_modules/`; a codegen template name (`CN<Name>...`); a Perforce path that may not be synced locally | INFO "anchor unresolved -- may be generated or unsynced; verify on a full sync"; **never FAIL** |
| **non-anchor** | a macro / keyword / concept-word, not a resolvable identifier: `UPROPERTY`, `UCLASS`, `UFUNCTION`, `GENERATED_BODY`, `checkNoEntry`, `SFAssert`, `__cpp_exceptions`, `DOREPLIFETIME` | skip entirely (no finding) |

**CD-1. anchor_modality_classify** (precondition, no severity). Tag every anchor per the table above. Emit nothing on its own; it gates CD-2/CD-3.

**CD-2. fidelity_anchor_resolves.** A **requires-present** anchor that is named-and-absent (after a repo-wide check) -> **FAIL** (taxonomy H). For a **requires-absent** anchor, run inverted: the asserted-absent thing is now *present* (a tracked file under a gitignored SSOT path; a FORBIDDEN name that now resolves) -> **FAIL** (taxonomy H2 -- the invariant is violated, surface it loudly). All other modalities -> PASS/INFO per the table, never FAIL.

**CD-2b. invariant_violated_by_code** (extension of `H2_inverted_absence`, added 2026-08-07). CD-2's inverted case already names this class -- a claim whose guarded condition is violated in the world, where "the fix is in the code/repo, not the CLAUDE.md". CD-2b generalizes it from a `requires-absent` anchor to a **stated invariant**. It emits under the SAME taxonomy id, `H2_inverted_absence`; do not invent a parallel id.

- **Subject.** An invariant the CLAUDE.md corpus STATES ("No silent fallbacks", "Comparability is never assumed"), checked against the code that invariant governs. The audited artifact is still the document; the finding is that the world the document describes has diverged from it. Unlike every other criterion here, the document is RIGHT and the code is WRONG.
- **Gate (this is what keeps it from generating speculation).** Two conditions, both required. (1) The invariant must be **quotable VERBATIM** from a CLAUDE.md in scope -- the subject file or an ancestor supplied to the run -- with exactly the rule-extraction posture H-11 states in section 2.4: no inferred invariants, no generic best practice, no "spirit of" a rule. If you cannot quote it verbatim, do not raise the finding. (2) The violation must be **demonstrable at a cited code location** (file plus symbol, read during the audit). A suspicion with no citation does not fire. A paraphrased invariant, or a verbatim one with no located violation, is not a finding.
- **Quote the smallest SELF-CONTAINED rule statement.** A bolded headline ("No silent fallbacks") may be quoted alone only when the body beneath it does not NARROW it. Where the body scopes the rule ("...hardcoded fallback values when config data is missing"), the body is the invariant and the quote must include it; firing on the broad headline while the body excludes the case at hand is the "spirit of the rule" reasoning gate 1 forbids. When headline and body disagree about whether a case is covered, that ambiguity belongs in the finding, not in a silent decision either way.
- **Scope: ancestors only.** "In scope" means the subject file plus the ancestor CLAUDE.md chain supplied to the run. A violation in a SIBLING or cousin subtree is out of reach and must not be reached for -- an invariant stated in `a/CLAUDE.md` is not checked against code under `b/`, even when both are in the same repo. This keeps the finding inside the ambient load graph the reviewer actually has, and stops the criterion from silently becoming a whole-repo sweep.
- **Dimension.** Runs on **both** dimensions. A stated invariant is not tied to either one -- a root CLAUDE.md declaring "No silent fallbacks" may classify `code-directory` (it sits beside build files, with no `claude_md:` block) or `classic` (it declares one), and the invariant reads identically either way. Restricting by dimension would make the finding depend on an unrelated property of the file that states it. This is therefore the one CodeDir-group finding that can fire on a `classic` file; nothing else in section 3 does.
- **Severity and disposition.** Severity **JUDGMENT**; disposition **SERIOUS**, always. **Never FIX.** The correction is in code the audit must not touch, and editing the doc to describe the violation would fossilize a bug as documented behavior.
- **Remediation owner: the code author, not the doc.** The `remediation` names the verbatim invariant, its source CLAUDE.md path, and the violating code location, and states that the CODE must be brought back into compliance (or the invariant deliberately retired by a human). It never proposes an edit to the CLAUDE.md.
- **Do not assert that the code is at fault.** "The document is RIGHT and the code is WRONG" is the framing that justifies never auto-fixing the doc, but it is not always the truth of the case: an invariant stating "two documented boundaries where this is allowed" against a third real boundary may mean the code drifted OR that the catalog is incomplete. Report the contradiction and both resolutions -- fix the code, or extend/retire the invariant -- and leave the choice to a human. The rule that never changes is that the AUDIT does not silently edit the doc to match the code, which would fossilize a defect as documented behavior.
- **Verdict interaction.** A correct document beside a violated invariant must NOT be marked NON-COMPLIANT -- the document has no defect, and gating it would be false. The finding must not vanish either. The existing precedent resolves both: **SERIOUS findings are reported ABOVE the verdict and survive review mode's attributability filter** (see the audit lane's "Review mode"). So the rule is: **a violated invariant is reported, never gated -- SERIOUS above the verdict, the verdict itself unchanged.** In normal mode the file stays COMPLIANT with the SERIOUS block first on the page; in review mode it stays DIFF-CLEAN with the SERIOUS surviving even though the change under review did not cause it. This is why the finding carries JUDGMENT severity and not FAIL: FAIL is the gating channel and would make the verdict lie about the document.

**CD-3. fidelity_line_anchor** (JUDGMENT, coupled to symbol resolution). When a claim cites a line number (`lines ~2204-2215`, `line 120`): find the enclosing symbol the claim names. If the symbol resolves but is **>~30 lines** from the cited number -> **I2_line_drift**, remediation "drop the line number; keep the symbol anchor" (AUTO). **Stay silent** if the author already supplied a recovery hint (e.g. "run `grep -n '# NOTE:'` before reviewing"). If no line number is cited, skip.

**CD-4. fidelity_claim_holds** (JUDGMENT, never auto-FAIL). Read the anchored code; is the claim still true **in kind**? A god-object now decomposed, a TODO the claim depends on now resolved, a "bypasses X" that no longer bypasses -> **I_claim_drift** (DISCUSS). **Counted magnitudes are intentionally fuzzy** -- "7200-line", "resets 10 fields", "12 C# files" -- **never FAIL on the number**; flag only if the *kind inverts* (god-object -> small/decomposed).

**Narrow exception -- exact-enumeration counts (2026-08-07).** A count the reader relies on as a COMPLETE list ("the eleven native systems", "the six unittest suites") is not a fuzzy magnitude; it is a checkable claim. Verify it by **enumerating the code** -- count the registration call sites, the test definitions, the directory entries -- and never by reading a comment or heading that restates the number, which is another copy of the claim rather than evidence for it. A wrong exact-enumeration count is emitted as `P_stale_factual_claim` (severity JUDGMENT on this dimension, disposition **FIX**: recount and correct it, preferring the information-preserving fix). CD-4 itself is unchanged: it still never FAILs on a number, and this exception adds no FAIL either -- see A-3's count-claims rule for the exact-enumeration / illustrative-magnitude test, the transitive-enumeration requirement, and the ambiguity tiebreak.

**Accepted recall cost (stated deliberately, not an oversight).** The tiebreak silences a bare parenthetical count of a derived artifact set -- "into 71 m4a clips" reads as illustrative under the test, and a measured regression run confirmed it stays silent while "the eleven native systems" fires. That miss is accepted: under an applied FIX disposition, a missed count costs a reader one stale number, whereas a false exact-enumeration costs a confidently rewritten wrong one. Do not "fix" this by loosening the tiebreak.

**CD-5. value_insight_earns_place** (JUDGMENT). Run each section through the value filter (section 3.5). Low-value -> **J_low_value_insight** (DISCUSS; a genuinely *bare* un-annotated inventory may be AUTO delete).

**CD-6. silent_failure_preserved** (INFO, positive check). If the file has been reduced to only structural description with **no** tier-1/tier-2 silent-failure or blast-radius claim, emit an INFO erosion signal -- the highest-value content may have been edited out.

**Severity and verdict interaction.**

- `CD-2` FAIL (H or H2 on an *anchor*) gates the file NON-COMPLIANT, same as a classic FAIL.
- `CD-2b` (H2 on a stated *invariant*) is JUDGMENT/SERIOUS and does NOT gate: it is reported above the verdict and leaves the verdict describing the document only.
- `CD-3`/`CD-4`/`CD-5` are JUDGMENT/DISCUSS and `CD-6` is INFO -- they surface for review without gating, and do not escalate to FAIL on re-run.
- An exact-enumeration count corrected under CD-4's narrow exception is `P_stale_factual_claim`, JUDGMENT severity with disposition FIX; it does not gate either.
- Idempotency: the modality classification and anchor resolution are mechanical; the JUDGMENT prompts are fixed. Same file + same tree -> same findings.
- A file with no `requires-present` anchors and no value-filter failures is **COMPLIANT** on this dimension even though it carries many claims -- absence of FAIL is the bar, exactly as for the classic dimensions.

**Code-directory finding taxonomy (extends the classic A-K):**

| ID | Name | Detection | Default remediation | Bucket |
|---|---|---|---|---|
| `H_stale_anchor` | requires-present anchor no longer resolves (repo-wide) | symbol/sibling/path absent and not external/generated/template/vendored | re-anchor to current symbol/path, or delete if the code is gone | DISCUSS |
| `H2_inverted_absence` | requires-absent thing is now present, OR a verbatim-quotable stated invariant is violated by cited code (CD-2b) | tracked file under a gitignored SSOT path; a FORBIDDEN name now resolves; a stated invariant contradicted at a cited code location | escalate as a finding -- the invariant is violated; CD-2b's remediation names the code author, never a doc edit | DISCUSS |
| `I_claim_drift` | claim no longer matches the code *in kind* | code read contradicts the claim (not a counted magnitude) | re-validate with user; update mechanism or retire the claim | DISCUSS |
| `I2_line_drift` | symbol found far from cited line, no recovery hint | enclosing symbol resolves >~30 lines from the number | drop the line number, keep the symbol | AUTO |
| `J_low_value_insight` | section fails the value filter (after carve-outs) | linter-caught / default / *bare* inventory / pure restatement | delete (bare inventory) or downgrade | DISCUSS (bare inventory delete -> AUTO) |
| `K_unclassified` | escape hatch | nothing above fits after a deliberate attempt | user proposes strategy | SPECIAL |

(Section 5.2 carries the same ids in the run-time disposition model the lanes use; the AUTO/DISCUSS/SPECIAL column above is the legacy bucket vocabulary and stays for continuity.)

### 3.7 Authoring direction

Write the file by running the standard forward: (1) pick the shape(s) (3.1); (2) write only the high-value kinds present **and silent** in this directory (3.2), using the two idioms where they apply (3.3); (3) anchor and path-reference per the discipline (3.4); (4) run every section through the value gate (3.5) **before writing it**, leading with silent-failure and blast-radius content ordered by the value lattice; (5) do not add a `claude_md:` block and do not run the schema validator on it.

---

## 4. Density lens (opt-in, advisory only)

The classic standards answer **where a fact lives** (which file, which scope). This lens answers the orthogonal question they only gesture at: **does a correctly-placed file carry more tokens than its information content needs, and should some of it be disclosed to a reference rather than inlined?** It is the operational form of CRP's "don't make a reader load what they don't need" -- applied at the section/block level, not the file level.

**Never runs by default.** A run without the `density` request never applies section 4 and emits no Density findings.

### 4.1 The overriding rule: density is not deletion

Every density finding must **route the tokens somewhere** -- tighten in place, extract to a reference, or merge a duplicate. A finding whose only effect is *removing* load-bearing nuance is wrong by construction; the lens compresses lossy prose, it does not delete signal. This is why **every criterion here is JUDGMENT severity and every taxonomy row is DISCUSS** -- none gate compliance, none auto-apply. Verbosity judgment is noisy and can silently strip nuance an author put there deliberately; a human confirms each call. **The lens never emits FAIL and never emits AUTO. A density-only audit is always COMPLIANT.**

Concretely, for every finding state **where the tokens go**: *tighten* -> the same information in fewer words, same file, same place; *extract* -> the block moves to a reference doc and a one-line pointer stays behind; *merge* -> one of N restatements survives and the others become a cross-reference. If you cannot name the destination, do not raise the finding.

### 4.2 Candidate identification

Read the file top to bottom and mark sections (a `##`/`###` heading and its body) that are *plausibly* over-weight. Cheap signals, none of which is a verdict on its own: a section materially longer than its neighbors for no structural reason; a worked example, schema dump, or recipe that a reader needs only sometimes; the same fact appearing in more than one section; preamble/ceremony/hedging ("it is important to note that...", "as always, be careful to...") that carries no testable content; a section that restates something a linter, the language, or an ancestor CLAUDE.md already enforces.

Marking is generous; the criteria below are where strictness lives.

### 4.3 The DD criteria

#### DD-1. density_in_place (JUDGMENT -> L_verbose_in_place, DISCUSS)

A section that is **correctly placed and carries real value** but says in N words what materially fewer would carry. Targets: redundant restatement within the section, over-explanation of the obvious, hedging/ceremony preambles, repeated re-establishment of context the reader already has. Output: a *tightened* rewrite (or a token-savings estimate + the specific sentences to cut/compress), **same file, same place**. Never propose moving or deleting the section -- that is DD-2 / DD-4.

**Carve-outs (do NOT flag):** a worked example that teaches a genuinely non-obvious procedure; load-bearing nuance that reads as redundant but guards a real failure mode (the author's "even though X, still do Y" is usually load-bearing); deliberate, labeled repetition of a safety rail. When unsure whether prose is ceremony or load-bearing nuance, leave it -- false-positive compression is the expensive error.

#### DD-2. extract_to_reference (JUDGMENT -> M_extract_to_reference, DISCUSS)

A **self-contained block** that (a) serves an on-demand or narrow reading task -- not every reader on every load needs it -- and (b) is large enough that inlining it taxes every reader who *doesn't*. The fix is **disclosure-level, not scope-level**: the block moves to a `references/*.md` (or a SKILL.md when it is on-task procedure) and a one-line pointer stays in the CLAUDE.md. This is the L1->L3 (or L1->L2) move.

Distinguish it from the classic criteria so findings don't double-count. **vs `crp_role_appropriate` (A):** A is *wrong scope* -- the content belongs in a different file in the role chain (a subdir CLAUDE.md). DD-2 is *right scope, wrong disclosure level* -- the content belongs to this scope but should sit one disclosure layer deeper. **vs `crp_size_signal` (F) / `C_crp_split_candidate` (C):** F is the mechanical whole-file size trigger; C is the structural "this whole file decomposes into L2/L3." DD-2 is the finer, block-level call -- *this one block* should be disclosed even when the file as a whole is fine. When C and DD-2 both fire, DD-2's per-block proposals are the concrete form of C.

#### DD-3. intra_file_redundancy (JUDGMENT -> N_intra_file_redundancy, DISCUSS)

The **same fact stated more than once within this one file** (distinct from `ccp_cross_file_duplication` (B), which is duplication across the role chain and is a FAIL/AUTO). Output: keep the single best statement; replace the others with a cross-reference. State once.

Boundary -- prose vs insight-record overlap: when one file carries both prose sections and structured records (e.g. a `claude_md:` insights block), summary-level overlap is acceptable -- a prose orientation and a record's `summary:` may state the same fact. The detail lives in exactly one place; flag only detail-level duplication.

#### DD-4. value_earns_tokens (JUDGMENT -> O_low_value_verbose, DISCUSS)

A section that **does not earn its tokens** under the value filter -- and is verbose about it. This is the **classic-file generalization** of the code-directory value filter. Rank kept content by the same lattice; for the canonical ranking and the carve-out list, defer to **section 3.5** (do not restate it here -- SSOT). Low-value-and-verbose -> propose downgrade (compress to a line) or, for a genuinely contentless section, deletion, **with the user's confirmation**.

**Do not double-count with the code-directory dimension.** If the file is `dimension: code-directory`, the value filter already runs as CD-5 (taxonomy J) -- let it own value findings there. DD-4 is for `classic` files (and the non-code sections of a mixed file), where no value filter otherwise runs.

### 4.4 What this lens does NOT do

- It does not move content between files in the role chain (that is `crp_role_appropriate` / A) -- only between *disclosure levels* within a scope.
- It does not invent new content or "improve" an author's voice; it reduces tokens against a fixed information content.
- It does not run by default.
- It never produces a FAIL or an AUTO. The lens surfaces opportunities, never gates.

Density findings are emitted under group **Density**, severity **JUDGMENT**, taxonomy one of `L_verbose_in_place` / `M_extract_to_reference` / `N_intra_file_redundancy` / `O_low_value_verbose`, bucket **DISCUSS**. Each finding's `remediation` MUST name the destination (tighten / extract->`<ref path>` / merge->`<surviving location>`) per 4.1. Include an approximate token-savings figure when proposing a cut or extract, so the user can weigh the trade.

---

## 5. Audit-direction mapping

The criteria and taxonomy ids the claude-md audit lane emits, verbatim. Ids are contract: the golden corpus keys on them.

### 5.1 Criteria

| id | name | severity | summary / detail |
|---|---|---|---|
| `ccp_change_cadence` | CCP -- content changes for the same reason | JUDGMENT | Each rule, insight, or convention in a CLAUDE.md belongs to that file only when it changes for the same reason as the file's role (project conventions for project-root CLAUDE.md, directory-local invariants for child CLAUDE.md, etc.). Judgment call per cohesion-principles `per_artifact_role.claude_md.audit_rules`. The agent reads the body and asks: does this content's change cadence match the file's role? Keywords: ccp, change cadence, single reason, content allocation. |
| `ccp_cross_file_duplication` | CCP -- no cross-file rule duplication along the role chain | FAIL | A rule stated in a parent CLAUDE.md (ancestor role) must not be restated in a child CLAUDE.md. The agent loads the parent automatically when descending into the child. Detected by reading the parent CLAUDE.md (when available) and comparing rule statements. Restated rules signal a misunderstanding of the load model. Keywords: ccp, duplication, parent rule, ancestor inheritance. |
| `crp_size_signal` | CRP -- body size as an evaluation prompt | INFO | A CLAUDE.md over the size threshold (500 lines / 3000 tokens approx) is a signal to evaluate whether sections serve different reading tasks; the threshold itself is not a verdict. Mechanical line/token count. Triggers a CRP-evaluation prompt; the agent runs the test (do sections serve different reading tasks?) before proposing a split. Keywords: crp, size threshold, split signal, progressive disclosure. |
| `crp_role_appropriate` | CRP -- content sits at the role with the smallest correct scope | JUDGMENT | A rule that applies only to a subdirectory belongs in that subdirectory's CLAUDE.md, not the project root. A rule that applies everywhere belongs in the root, not duplicated per subdirectory. Judgment call from cohesion-principles: what is the smallest scope where this rule is correct? Place it there. Keywords: crp, role scope, smallest correct scope, wrong role. |
| `adp_no_forward_dependency` | ADP -- no forward dependency on descendant CLAUDE.md content | FAIL | Parent (root or ancestor) CLAUDE.md must not depend on or reference descendant CLAUDE.md content. The load graph flows root -> ancestor -> child, one direction. Detected by scanning the body for descendant-path references or load-time assumptions about subdir CLAUDE.md content. Keywords: adp, forward dependency, dag, descendant reference. |
| `hygiene_thresholds` | Hygiene -- universal field and length rules | INFO | Body length, broken markdown links, and other universal structural rules. Most are INFO severity unless they cross a hard threshold. Mechanical universal rules. Distinct from CRP -- hygiene checks structural correctness; CRP checks placement intent. Keywords: hygiene, line count, token count, structural rules. |
| `schema_validation` | claude_md: YAML block validates against schema (when present) | FAIL | Files carrying a `claude_md:` YAML contract block in the body must validate against `CLAUDE_MD_SCHEMA` in schemas.py. Files without the block are not gated on schema validation. Mechanical validation via audit.py when the block is present. Conditional: applies only when the file declares the contract. Root-role files SHOULD carry the block (the authoring direction adds one when it touches a root file); absence on a pre-existing root file is surfaced as INFO, never FAIL. Keywords: claude_md schema, yaml validation, optional contract, claude-md schema. |
| `cd_anchor_modality_classify` | CodeDir -- classify every anchor's modality before any existence check | JUDGMENT | For a code-directory file, tag each concrete anchor (symbol / file / sibling / field / name) with exactly one modality FIRST. Only `requires-present` is eligible for FAIL; `requires-absent` scores inverted; external / template-or-env / vendored / generated-or-unsynced / non-anchor never FAIL. Precondition for cd_fidelity. The Level-2 safety valve: because the Level-1 trigger fires generously, modality classification is what prevents false FAILs on negative-existence, external, templated, and generated anchors. Full table in section 3.6. |
| `cd_fidelity_anchor_resolves` | CodeDir -- claim anchor resolves (or, for requires-absent, stays absent) | FAIL | A `requires-present` anchor that is named-and-absent after a repo-wide check is a FAIL (H_stale_anchor). A `requires-absent` anchor whose asserted-absent thing is now present is a FAIL (H2_inverted_absence -- the invariant is violated). All other modalities are PASS/INFO. Mechanical resolution: symbols repo-wide, leading-slash paths against repo root. Conditional on dimension=code-directory. This is the only CD criterion that gates compliance. CD-2b extends the same H2 taxonomy id to a verbatim-quotable STATED invariant violated by cited code -- that variant runs on both dimensions, is JUDGMENT/SERIOUS, and never gates (section 3.6, CD-2b). |
| `cd_fidelity_line_anchor` | CodeDir -- cited line number tracks its symbol | JUDGMENT | When a claim cites a line number, find the enclosing symbol it names; if the symbol resolves but is >~30 lines from the cited number, flag I2_line_drift (drop the number, keep the symbol). Stay silent if the author supplied a recovery hint. Coupled to symbol resolution; never fires when no line number is cited. Disposition FIX (the remediation is the mechanical removal of the stale number -- a convention-violation fix). |
| `cd_fidelity_claim_holds` | CodeDir -- claim still matches the code in kind | JUDGMENT | Read the anchored code; if the claim no longer holds in kind (god-object now decomposed, TODO now resolved, bypass now gone) flag I_claim_drift. Counted magnitudes ("7200-line", "12 files") are intentionally fuzzy -- never FAIL on the number; flag only on kind-inversion. Never auto-FAIL. Narrow exception (2026-08-07): an EXACT-ENUMERATION count -- one the reader relies on as a complete list ("the eleven native systems") -- is verified by ENUMERATING the code (registration call sites, test definitions, directory entries), never against an adjacent comment restating the number, and a wrong one is emitted as P_stale_factual_claim, JUDGMENT severity with disposition FIX. See A-3's count-claims rule. Disposition FIX when the audit verified the actual behavior from the code reading (the code reading is evidence; intent re-derivation is not a blocker); IMPROVE only when no fact decides the correct claim. |
| `cd_value_insight_earns_place` | CodeDir -- section earns its place under the what-we-care-about filter | JUDGMENT | Each section must pass the value lattice (silent-failure > blast-radius > deliberately-wrong > safety > perf > ownership). Linter-caught / default / bare-inventory / pure-restatement sections are low-value (J). Honor every carve-out: annotated Files/Schema blocks, SSOT-pointing catalogs, safety-rail cheatsheets, topology tables are NOT low-value. Disposition FIX for a bare un-annotated inventory / default / restatement (delete, be aggressive); IMPROVE for a trim of true content passing the one-line test; SILENT for a validator artifact or accepted structural pattern. Carve-outs are load-bearing -- both maintainer-agents required them. |
| `cd_silent_failure_preserved` | CodeDir -- highest-value content still present | INFO | Positive check: if the file has been reduced to only structural description with no tier-1/tier-2 silent-failure or blast-radius claim, emit an erosion INFO -- the highest-value content may have been edited out. Advisory only; never gates. Surfaces value erosion across edits. |
| `dd_density_in_place` | Density -- correctly-placed, valuable section is over-worded | JUDGMENT | A section that is correctly placed and carries real value but says in N words what materially fewer would carry (redundant restatement, hedging/ceremony preamble, over-explanation of the obvious). Remediation tightens IN PLACE; never moves or deletes. Honors carve-outs (load-bearing nuance, teaching examples, labeled safety-rail repetition). Opt-in density lens, loaded only on the `density` request. Advisory: never FAIL. DD-1. Disposition IMPROVE; remediation must route tokens (tighten in place). |
| `dd_extract_to_reference` | Density -- self-contained block should be disclosed to a reference | JUDGMENT | A self-contained block serving an on-demand/narrow reading task, large enough that inlining taxes every reader, should move to a references/*.md (or SKILL.md for on-task procedure) leaving a one-line pointer. Disclosure-level move, not scope-level -- distinct from crp_role_appropriate (wrong file) and finer than C_crp_split_candidate (whole-file split). DD-2. Advisory: never FAIL. Disposition IMPROVE; remediation names the destination reference and the pointer left behind. |
| `dd_intra_file_redundancy` | Density -- same fact stated more than once within one file | JUDGMENT | The same fact stated multiple times within THIS file (distinct from ccp_cross_file_duplication, which is across the role chain and FAIL/FIX). Keep the single best statement; replace the others with a cross-reference. DD-3. Advisory: never FAIL. Disposition IMPROVE; remediation names which statement survives. |
| `dd_value_earns_tokens` | Density -- section does not earn its tokens (classic-file value filter) | JUDGMENT | The classic-file generalization of the code-directory value filter: a section that does not earn its tokens under the value lattice AND is verbose about it. Defers the ranking + carve-outs to section 3.5 (SSOT). Does NOT double-count with CD-5/J -- on a code-directory file, value findings stay in CD-5. DD-4. Advisory: never FAIL. Disposition IMPROVE; remediation proposes downgrade-to-a-line or confirmed deletion of a contentless section. |

### 5.2 Finding taxonomy

Disposition is instance-level, not a fixed property of the taxonomy id: `bucket` is only the DEFAULT starting point; the detect classifier assigns FIX / SERIOUS / IMPROVE / SILENT per finding against explicit predicates. Same file, same finding -> same disposition (idempotent).

| id | name | detection_signal | default_remediation | bucket |
|---|---|---|---|---|
| `A_wrong_role_content` | Content sits at the wrong role in the CLAUDE.md hierarchy | Agent judgment from the role-to-criteria map. Body section's scope is narrower or broader than the file's role allows. | Propose moving the section to the correct-scope CLAUDE.md (e.g. narrow root rule -> subdirectory CLAUDE.md; broad subdir rule -> project root CLAUDE.md). User confirms the move. | IMPROVE |
| `B_ccp_cross_file_duplication` | Rule restated from parent CLAUDE.md | Body restates a rule already present in an ancestor CLAUDE.md (read during the audit's role-walk phase). | Delete the restated rule from the child file (the parent rule loads automatically when the agent descends into the child). Apply the loss-free-deletion guard first: diff the restated rule against the parent copy and fold any child-local delta into the parent SSOT (or a REMINDER-PLUS-REFERENCE summary line of a dozen tokens or less naming it) before deleting. | FIX |
| `C_crp_split_candidate` | Body sections serve different reading tasks (CRP split warranted) | Body over size threshold AND agent judgment that sections genuinely serve different reading tasks (e.g. setup-time rules + on-task triggers + reference glossary). | Propose an L1 -> L2 / L3 decomposition: move on-task content to a SKILL.md (L2); move reference content to a reference doc (L3). User confirms before splitting. | IMPROVE |
| `D_adp_forward_dependency` | Parent CLAUDE.md depends on descendant content | Body references or assumes content from a descendant CLAUDE.md (e.g. "see subsystem/CLAUDE.md for the rule"). | Either inline the descendant content into the parent (if the rule is truly parent-scoped) or remove the forward reference (if the rule is descendant-scoped and the parent has no business assuming it). User confirms. | IMPROVE |
| `E_schema_failure` | claude_md: YAML block fails schema validation | audit.py reports schema validation failure for the file's claude_md: YAML block (missing required key, wrong type, forbidden key). | Surface the failing rows. A missing field with a sensible default is decidable by convention -> FIX (add it). A field requiring authorial judgment -> IMPROVE (offer it as a one-liner). | FIX |
| `F_hygiene_threshold` | Body over size threshold (CRP-evaluation prompt) | Mechanical INFO finding: body line count > 500 or token count > 3000. | Run the CRP test (do sections serve different reading tasks?). If yes, escalate to C. If no, INFO stays as-is. | IMPROVE |
| `G_descendant_role_mismatch` | Local file (.local) carries non-local content | CLAUDE.local.md body contains project-conventional content that should be in the checked-in CLAUDE.md instead of a personal override. | Propose moving the project-conventional content to the checked-in CLAUDE.md (so all collaborators see it). User confirms before moving. | IMPROVE |
| `P_stale_factual_claim` | A numeric count or checkable factual claim is contradicted by current repo state | A-3 stale-reference hit: a numeric count or other checkable factual claim in a classic (non-code-directory) CLAUDE.md (e.g. "the six unittest suites") is contradicted by current repo state (e.g. seven test files exist). Scope extended 2026-08-07: also fires on a CODE-DIRECTORY file, but there for EXACT-ENUMERATION count claims only (a count the reader relies on as a complete list) -- illustrative-magnitude counts stay exempt per section 3.4 / CD-4, and ambiguous cases are treated as illustrative. Verification is by ENUMERATING the code (registration call sites, test definitions, directory entries); a comment or heading restating the number is another copy of the claim, not evidence, and checking against it confirms a wrong doc. Severity FAIL on a classic file as before; JUDGMENT on a code-directory file, where only CD-2 gates. | FIX when the fix is a mechanical count/value update with unambiguous ground truth (recount and correct the number; prefer the information-preserving fix -- correct the count AND add the missing entry). IMPROVE when the discrepancy might be intentional (the count is aspirational or the claim is ambiguous) -- offer it as a one-liner. | FIX |
| `Q_skill_content_duplication` | CLAUDE.md restates content a skill owns | C-6 hit: a substantial block in a CLAUDE.md (or a project reference doc it cites) restates content owned by a skill or skill reference (verbatim or near-verbatim). NOT B, which is ancestor-CLAUDE.md restatement. | Trim to a one-line guardrail naming the rule or failure mode plus a pointer to the skill (per C-5/A-4); the skill carries the depth. This is dedup under the summarize-and-reference rule (REMINDER PLUS REFERENCE): keep an inline reminder of a dozen tokens or less plus the pointer to the SSOT skill, reference-only beyond that budget. Apply the loss-free-deletion guard first -- fold any local delta into the guardrail line before trimming. | FIX |
| `R_ancestor_convention_violation` | Subject violates a convention an ancestor CLAUDE.md explicitly declares (H-11) | H-11 hit: the subject violates a convention EXPLICITLY declared in an ancestor CLAUDE.md (loaded ambient), and the exact declared rule is quotable VERBATIM from that ancestor (no inferred / generic conventions). Fires only when ancestorClaudeMdPaths is supplied and non-empty; a root file with no ancestors emits nothing. | FIX for a mechanical correction against the documented convention (replace a non-ASCII look-alike, relativize a hardcoded absolute path, apply the stated formatting rule); the message carries the verbatim ancestor rule quote + the ancestor source path. SERIOUS when the violation reveals a real-world problem the rule exists to prevent (e.g. a committed secret an ancestor forbids) -- surfaced at the top, never auto-fixed. | FIX |
| `H_stale_anchor` | CodeDir: requires-present anchor no longer resolves | A `requires-present` anchor (symbol / file / sibling / field the claim says should exist) is absent after a repo-wide check, and is not classified external / generated / template / vendored. | Re-anchor the claim to the current symbol/path (FIX -- the target mechanism was found), or delete the claim if the code it describes is gone (FIX -- deleting falsified content loses nothing). SERIOUS instead when the stale anchor is a protective rail with NO surviving mechanism -- the real finding is the unprotected invariant. | FIX |
| `H2_inverted_absence` | CodeDir: requires-absent thing is now present, or a stated invariant is violated by the code (CD-2b) | (a) CD-2: a `requires-absent` claim's asserted-absent thing now exists (a tracked file under a gitignored SSOT path; a FORBIDDEN name that now resolves) -- severity FAIL, gates. (b) CD-2b: an invariant the corpus STATES is contradicted by code, subject to both gates -- the invariant is quotable VERBATIM from the subject or a supplied ancestor CLAUDE.md (H-11 posture; no paraphrase, no inference), and the violation is demonstrable at a CITED code location (file + symbol). Runs on both dimensions -- the one CodeDir-group finding that may fire on a `classic` file. Severity JUDGMENT; does not gate. | Surface loudly at the TOP of the report as a SERIOUS finding -- the invariant the claim guards is violated; the doc problem reveals a real-world problem. The fix is in the code/repo, not the CLAUDE.md. Never auto-fixed. For CD-2b the remediation owner is the CODE AUTHOR: name the verbatim invariant, its source CLAUDE.md path, and the violating code location, and say the code must be brought back into compliance (or the invariant deliberately retired by a human); never propose a CLAUDE.md edit. Verdict interaction: a correct doc beside a violated invariant stays COMPLIANT (DIFF-CLEAN under review) -- the finding is reported ABOVE the verdict and survives review mode's attributability filter, so it is never gated and never lost. | SERIOUS |
| `I_claim_drift` | CodeDir: claim no longer matches the code in kind | Reading the anchored code contradicts the claim in kind (decomposed god-object, resolved TODO, bypass now gone). NOT a counted-magnitude difference. | Update the mechanism/magnitude or retire the claim. FIX when the audit verified the actual behavior from the code reading -- intent re-derivation is not a blocker, the code reading is evidence. IMPROVE only when the correct claim cannot be decided from a fact (offer as a one-liner). | FIX |
| `I2_line_drift` | CodeDir: cited line number drifted from its symbol | The enclosing symbol the claim names resolves but is >~30 lines from the cited number, and the author gave no recovery hint. | Drop the line number; keep the symbol anchor. Mechanical convention-violation fix. | FIX |
| `J_low_value_insight` | CodeDir: section fails the what-we-care-about value filter | A section is linter-caught / a language default / a bare un-annotated inventory / a pure schema restatement -- AND not protected by a carve-out (annotated Files/Schema, SSOT-pointing catalog, safety-rail cheatsheet, topology table). | FIX for a bare un-annotated inventory / language default / pure restatement / linter-caught content -- delete it (be aggressive; a default trim is decidable). IMPROVE when the section carries TRUE content that passes the one-line test (offer the trim as a one-liner). SILENT for a validator detection artifact or an accepted structural pattern (historical record, agent-definition file). Apply the loss-free-deletion guard before any deletion. | FIX |
| `L_verbose_in_place` | Density: correctly-placed section is over-worded | Density lens (DD-1): a valuable, correctly-scoped section uses materially more words than its information content requires, and is not protected by a carve-out (teaching example, load-bearing nuance, labeled safety-rail repetition). | Propose a tightened rewrite (or the specific sentences to compress) IN PLACE, with an approximate token-savings figure. Never move or delete. Offer as a one-liner (trim of true content passing the one-line test). | IMPROVE |
| `M_extract_to_reference` | Density: self-contained block should move to a reference | Density lens (DD-2): a self-contained on-demand block is large enough to tax every reader who does not need it; it belongs one disclosure level deeper (references/*.md or a SKILL.md) within the same scope. | Propose moving the block to a named reference doc and leaving a one-line pointer behind, with an approximate token-savings figure. A structural (disclosure-level) move -- offer as a one-liner. | IMPROVE |
| `N_intra_file_redundancy` | Density: same fact stated more than once within one file | Density lens (DD-3): a fact is restated in multiple sections of THIS file (not across the role chain -- that is B). | Propose keeping the single best statement and replacing the others with a cross-reference (the summarize-and-reference rule, within one file). Offer as a one-liner; the density lens stays advisory. | IMPROVE |
| `O_low_value_verbose` | Density: section does not earn its tokens (classic-file value filter) | Density lens (DD-4): a classic-file section fails the value lattice (section 3.5) AND is verbose. Not run on code-directory files (CD-5/J owns value there). | Propose downgrade (compress to a line) or, for a contentless section, deletion. Offer as a one-liner; this opt-in lens stays IMPROVE and never auto-deletes. | IMPROVE |
| `K_unclassified` | Unclassified / special case | Finding does not match any A-G, P, Q, or H-J detection signal after deliberate attempt. | Surface to the user with the audit row that fired, attempted matches, and reasons none fit. User proposes strategy. | SPECIAL |
| `N_user_standard_violation` | CLAUDE.md violates a user-authored standards criterion (standards_set) | A criterion from a resolved *-standards.md (standards_set) governing the claude_md primitive is violated, with the criterion statement quotable VERBATIM from the standards file. Judgment criteria only (enforcement judgment or absent); enforcement: mechanical criteria are audit.py's job under --config, not the lane's. Fires only when standardsPaths is supplied and non-empty. Suppressed when the criterion id is in disabledCriteria. (The N_ letter is shared verbatim across the former md-audit members' taxonomies for this cross-cutting user-standards category; it is distinct from N_intra_file_redundancy in this artifact's own taxonomy -- the suffix disambiguates.) | Disposition follows the criterion's declared severity: fail -> SERIOUS (a hard user-declared rule the auditor cannot mechanically satisfy -- surfaced at the top, never auto-fixed; the message carries the verbatim statement + criterion id + source standards-file path); info -> IMPROVE (one-line pitch); judgment -> JUDGMENT (surfaced for review). An arbitrary user standard is not mechanically fixable, so N is never auto-applied. | SERIOUS |

### 5.3 Gating

FAIL findings (CCP cross-file duplication, ADP forward dependency, schema validation failures with non-optional missing fields, CD-2, an H-11 ancestor-convention violation, and an N_user_standard_violation of a fail-severity criterion) gate compliance. JUDGMENT findings surface for review without gating; INFO findings are advisory only. JUDGMENT findings are resolved by user confirmation (PASS once the user accepts the exception explicitly); FAIL findings have no bypass -- remediation is available within the taxonomy.

**Reported-but-not-gated.** Two findings are deliberately SERIOUS-without-FAIL: CD-2b (a stated invariant violated by code) and any SERIOUS whose subject is the world rather than the document. They are reported ABOVE the verdict and survive review mode's attributability filter, while the verdict continues to describe only the document. Gating on them would assert a defect in a document that has none; suppressing them would lose the most important thing on the page. Reported, never gated.

---

## 6. Generation-direction notes

The standards facts the producing lane (author and generate) produces against. The lane's *procedure* (which file to touch, when to invoke cohesion-principles, how to validate) lives in `../lanes/generation-lane.md`; what follows is the artifact contract it satisfies.

### 6.1 The `claude_md:` block

A classic CLAUDE.md carries a `claude_md:` YAML block. It is the load-bearing structured surface; the schema is validated by `skills_kit_lib.audit` (run via the plugin venv) and its canonical shape is `CLAUDE_MD_SCHEMA`. Required shape:

- **`scope.covers`** -- what this file owns.
- **`scope.excludes`** -- **load-bearing, strongly expected**: the exclusion clause is what stops adjacent areas from drifting into this file's ownership. The machine schema (`skills_kit_lib/schemas/claude_md.py`, which wins on divergence) marks it optional, so omitting it is an authoring-quality finding (IMPROVE), not a schema FAIL.
- **`insights`** -- records, each carrying `id`, `keywords` (**at least 3** entries; the floor exists for chat-term routing), `summary`, `detail`, `origin`, `added`.
- **`conventions`** / **`glossary`** -- optional; add only if the shape calls for it.

**Root-role files SHOULD carry the block.** When generation touches a root CLAUDE.md that lacks one, add it. (The audit direction treats absence on a pre-existing root file as INFO, never FAIL -- adding the block is the generation path's job.)

**Schemas are floors, not ceilings:** the schema names the required minimum; an author may add load-bearing structured keys beyond it.

### 6.2 Which artifact, which direction

- A code-directory review-notes file carries **no** `claude_md:` block -- do not run the schema validator on it, and author it per section 3 instead. Treating one kind like the other (in either direction) is the recurring error.
- Skill-contract content belongs in a SKILL.md (see `skill-standards.md`), not a CLAUDE.md. A reference body belongs in a skill's `references/`, not a CLAUDE.md.
- **Where a fact lives** across the load graph is not re-derived here: it is the placement question, answered by `../cohesion-principles.md` (CCP change-cadence -> CRP reader-set -> ADP load-order). A placement that arrives already resolved (an audit remediation naming the destination, an orchestrator directive) is followed, not re-derived.
- **What shape a fact takes** (structured YAML record vs prose vs frontmatter) is answered by the authoring-pattern references (`../authoring-patterns/`): structured records over prose, because structure asserts completeness. Match the surrounding CLAUDE.md's existing format and SSOT -- extend an existing record rather than duplicating.

### 6.3 The generation anti-pattern

**Same fact in two CLAUDE.mds.** Putting the fact in both the root and the subsystem CLAUDE.md feels like it guarantees the reader sees it. It does not: two copies drift independently and CCP/SSOT is broken (this is exactly what C-1 / C-2 / `B_ccp_cross_file_duplication` detect from the other direction). The placement algorithm yields exactly one home; if sibling scopes also need the fact, bubble it up to the common parent -- still one copy.

The other recurring generation defects, stated as standards rather than procedure: a root CLAUDE.md with no `claude_md:` block; a missing `scope.excludes`; an insight record with fewer than 3 keywords; line-only anchors in a code-directory file (prefer a symbol anchor; drop the number unless the gotcha is sub-function -- section 3.4).

### 6.4 Retention marking (regeneration)

REGENERATION is `generate x claude-md` over a document that already exists. It keeps only what it can justify, and every unit of existing content falls into exactly one of three dispositions:

| Disposition | Condition | Outcome |
|---|---|---|
| VERIFIED | re-derived from current code by this run's coverage | kept, anchors refreshed |
| RETAINED | carries an explicit retention marking | kept VERBATIM, never re-worded |
| UNVERIFIED | neither of the above | reported; removed only on the user's say-so |

**The marking has two forms, because prose has no key to hang a field on.**

- Inside the `claude_md:` block, a record carries `retain: true`. It is valid on any record in `insights`, `conventions`, or `glossary`.
- Outside the block, an HTML comment on its own line marks the section that follows it -- from that line to the next heading of the same or shallower level:

```
<!-- md-domain: retain -->
```

**`retain: true` means "keep this even though it cannot be re-derived from code".** It is not a quality claim, not a correctness claim, and not a pin against a human editing the file. It instructs the regenerator and nothing else. Content that CAN be re-derived does not need it; marking such content is harmless and pointless.

**The first pass over an unmarked document PROPOSES markings and writes nothing.** A hand-written CLAUDE.md carries no markings by construction, so a regenerator applying the table above literally would delete all of it -- which is the failure this section exists to prevent. The first run therefore reports, per unit: what it verified, what it could not, and which unverifiable units it proposes to mark `retain`. The user marks them, or accepts the proposed set; only a subsequent run writes. A document this lane has already generated cannot land in that state, because every unverifiable unit it kept was marked at the time it was written.

**The proposal round is DETECTED, never configured.** It fires when the document exists and carries no retention marking of either form. There is deliberately no flag to skip it and none to force it: a flag would make the user restate something the lane can observe directly, and would become a second source of truth free to disagree with the file. See the plugin-opinion razor's preference for detecting an observable fact over configuring a preference.

**An UNVERIFIED unit is never silently dropped.** Reporting it is the point -- an unverifiable claim in a CLAUDE.md is either stale (delete it), a fact about intent rather than code (mark it), or a defect in the analysis (fix the analysis). Deleting it without saying so destroys the signal that distinguishes the three.
