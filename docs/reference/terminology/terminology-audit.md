# Terminology audit -- md-domain, git-code-review, p4-code-review

Audit of the three plugins' vocabulary against the owner's terminology framework
(2026-08-09). **Deliverable is the plan; no rename was applied.** Every claim
cites `path:line` against the working tree on `dev` at the time of the audit
(skills-kit 0.44.0, git-kit 0.13.0, p4-kit 0.27.0).

## The framework, as given

- **AUDIT** = making sure something is accurate and compliant.
- **A CODE REVIEW IS AN AUDIT OF A DIFF** -- a subtype of audit, not a sibling.
- **GENERATION** = creating a document that does not exist.
- **REGENERATION** = generation with preservation of existing value.
- Code introspection leading to fact discovery and documentation belongs in
  GENERATION / REGENERATION, not audit.

Fixed points, not relitigated: md-domain does not solicit review mode from a
user; md-domain does not offer to enumerate skills.

---

## 1. Framework-fit verdict

### 1.1 `audit` stays `audit` -- CONFIRMED, no change

The audit verb's own definition already matches the framework. `audit-framework.md:74`
defines FAIL as "gates compliance"; the verdict pair is COMPLIANT / NON-COMPLIANT
(`SKILL.md:156`). Accuracy is covered too -- CD-1..CD-6 own "fidelity and value of
present content" (`coverage-standards.md:143-145`). "Accurate and compliant" is a
faithful two-word summary of what the audit verb does today.

### 1.2 `author` is really GENERATION -- HOLDS, with one hard blocker

The reading holds against the code. The authoring lane already implements both
halves of the framework's generation/regeneration pair, and it already implements
them as ONE procedure with the existing-target case folded in:

- Generation (target does not exist): `authoring-lane.md:33` "Produce an artifact";
  `SKILL.md:235` phrasing "author a new skill"; `SKILL.md:249-251` "write a
  claude_md block", "author a CLAUDE.md for this directory".
- Regeneration (preserving existing value): `authoring-lane.md:29` the procedure is
  titled "author **or refine** an md artifact"; `authoring-lane.md:153` "extend an
  existing record rather than duplicating one"; `authoring-lane.md:155-156` the
  summarize-and-reference rule with its loss-free-deletion guard -- which IS
  "preservation of existing value", stated as a guard; `SKILL.md:236` "refine this
  SKILL.md".

So the verb-level collapse is real: `author` == generation, with regeneration as the
case where the target exists. Nothing in the lane needs a behavioural change to
satisfy the framework; the divergence is naming only.

**The blocker is a name collision, not a concept mismatch.** See section 3.2. In
short: "generated" is already taken, in a contract-bearing and load-bearing sense,
by the code-review kits and by cohesion-principles -- where it means *machine-emitted,
NOT reviewed, review belongs on the generator*. A CLAUDE.md md-domain writes is the
exact opposite: hand-maintained knowledge that IS reviewed. A literal `author` ->
`generate` rename makes those two meanings share a word across plugins that talk to
each other.

### 1.3 `coverage` is NOT a peer verb -- HOLDS. But it is not generation either.

The framework's attack on coverage's peer-verb status succeeds, and it does so
without touching the three contract facts that `coverage_is_a_report_only_third_verb`
rests on. This is worth stating precisely, because the brief asked which fact the
framework overturns:

The three verified facts at `md-domain/CLAUDE.md:141-153` are all arguments that
coverage is **not a fifth AUDIT lane** -- `audit-lane.md:19-22` (the pre-references
material applies to the three per-file lanes), `audit-lane.md:485-487` (idempotency
is an audit invariant that coverage disclaims), and
`test_domain_members_resolve.py:213-217`/`:236` (every `audit_*` lane but references
must declare NOT-AUDITED + DIFF-CLEAN and bind a `workflow_remediate`). **The
framework overturns none of them.** It agrees coverage is not an audit.

What the framework overturns is the *inference drawn from them*: the summary line at
`md-domain/CLAUDE.md:126` -- "a REPORT-ONLY third verb" -- treats "not an audit lane"
as establishing "therefore a peer verb". Under the framework, "not an audit" leaves
exactly one other home available (generation / regeneration), and coverage's own
description places it there: it reads code, discovers facts, and proposes
documentation (`coverage-lane.md:20-21`, "a fact about the code that belongs in a
CLAUDE.md and is not ambient for the code it describes"). The one sentence to treat
as superseded is `md-domain/CLAUDE.md:126`, not the three facts beneath it.

**Where it breaks: coverage produces no document.** Generation is defined as
*creating a document*. Coverage creates nothing -- `coverage-lane.md:70` "Nothing is
ever applied", `coverage-lane.md:189` "Then STOP", `coverage-standards.md:231-232`
"Coverage is report-only. It proposes a destination; it never writes one". So under
the framework as literally stated, coverage is neither audit nor generation. See
section 3.1 for what the framework needs.

**Answering the brief's specific question -- generation or regeneration?** BOTH, and
the discriminator is per-candidate rather than per-run. Each candidate names its own
destination CLAUDE.md chosen by the placement algorithm (`coverage-lane.md:178-180`),
and the discovery script reports an ambient chain that may be empty
(`coverage-lane.md:115-116`, "An empty `ambientClaudeMdPaths` ... is the strongest
possible finding"). A destination that does not exist yet is GENERATION; a
destination that exists and must keep its current content is REGENERATION. One run
routinely yields both, because CV-3 (`coverage-standards.md:66-77`) places each
candidate at its own ancestor. A per-run label would be wrong.

### 1.4 Net answer

The two-verb collapse **holds**, with two qualifications that are findings rather
than failures: (a) the umbrella name cannot be `generate` without colliding with an
existing cross-plugin meaning (3.2); (b) `coverage` collapses into the
generation/regeneration *family* but not into the generation *verb*, because it
writes nothing -- it is that family's discovery phase, and the framework has no term
for a phase (3.1).

---

## 2. Divergence table

Severity key: **MISLEAD** = the wrong word actively misleads a reader or contradicts
another statement in the same tree; **INCONSIST** = merely off-framework.

Blast-radius key: **P** prose only; **C** contract (lane id / verdict / rule id /
schema field / CLI token / test assertion / cross-plugin path).

### Group A -- `author` should be generation/regeneration

| # | path:line | current term | framework term | blast radius | sev |
|---|---|---|---|---|---|
| A1 | `plugins/skills-kit/skills/md-domain/SKILL.md:9` | `argument-hint: "[audit\|author\|coverage] ..."` | `[audit\|generate\|...]` | **C** -- user-typed CLI token. Consumers: `plugins/skills-kit/README.md:19`; `plugins/prototypes/skills/claude-explorer/SKILL.md:119` (`/md-domain author skill`); `cohesion-principles.md:14`. A rename is a user-visible break with no alias (the fold took a clean break on aliases, `md-domain/CLAUDE.md:46-48`). | INCONSIST |
| A2 | `SKILL.md:124-127` dispatch rows `author x skill \| claude-md \| project-doc` | `author x <artifact>` | `generate x <artifact>` | **C** -- the row KEYS are parsed and asserted verbatim by `tests/skills-kit/test_domain_members_resolve.py:162-188` (`f"{verb} x {artifact}"`). | INCONSIST |
| A3 | `SKILL.md:228,242,256` lane ids `author_skill` / `author_claude_md` / `author_project_doc`; `verb: author` at `:229,243,257` | `author_*` | `generate_*` | **C** -- lane ids. Consumers: `test_domain_members_resolve.py:62-64,144-155,258-263`; `scripts/gen_workflow_js.py:339` and its GENERATED output `workflow/project-doc-remediate.js:93` (a rename requires regenerating that workflow file, not editing it); `gen_workflow_js.py:304`. | INCONSIST |
| A4 | `references/lanes/authoring-lane.md` (whole file, path) | `authoring-lane.md` | `generation-lane.md` | **C** -- bound path in three lane records (`SKILL.md:232,246,260`), asserted as an exact string at `test_domain_members_resolve.py:262`, and indexed at `SKILL.md:451`. Also cited from `audit-lane.md:535`, `skill-standards.md:740`, `claude-md-standards.md:624`. | INCONSIST |
| A5 | `SKILL.md:450-453` index id `authoring_lane`; `:438-441` `authoring_standards`; `:458-461` `authoring_patterns` (+ dir `references/authoring-patterns/`) | authoring-* | generation-* | **C** for the ids (index ids are read by lanes), **P** for the summaries. | INCONSIST |
| A6 | `SKILL.md:14-16`, `:62-64`, `:76-80`, `:354`, `:379`, `:508` | "author", "authoring" as the verb name | generation / regeneration | **P** | INCONSIST |
| A7 | `plugins/skills-kit/CLAUDE.md:3` "two verbs (**audit**, **author**)"; `:7`; `:56` | author | generate | **P** | INCONSIST |
| A8 | `plugins/skills-kit/README.md:7,14,19,28,35,54,104` | author / authoring | generate / generation | **P**, but `:19` shows a copy-pasteable command, so it tracks A1. | INCONSIST |
| A9 | `references/cohesion-principles.md:13-14` "the authoring lanes reached via `/md-domain author skill\|claude-md\|project-doc`" | author | generate | **P** + a command string tracking A1. | INCONSIST |
| A10 | `references/lanes/authoring-lane.md:3-4`, `:99` "PRODUCING direction" / "producing direction" | producing | generating | **P**. Note this is the one place the framework's distinction is ALREADY drawn correctly in substance -- same standards doc, read in the other direction. | INCONSIST |
| A11 | `authoring-lane.md:29` "author **or refine** an md artifact" | author / refine | generate / regenerate | **P** -- and this is the highest-value prose row in Group A: the two halves of the framework are already here, unnamed. Naming them is nearly free and makes the framework visible where it operates. | INCONSIST |

### Group B -- `coverage` framed as a peer verb

| # | path:line | current term | framework term | blast radius | sev |
|---|---|---|---|---|---|
| B1 | `SKILL.md:14`, `:24`, `:354`, `:379` "three verbs" | coverage is a third peer verb | coverage is the discovery phase of generation/regeneration | **P** | MISLEAD -- it tells a reader the three are the same kind of thing, which is exactly what the framework denies. |
| B2 | `references/lanes/coverage-lane.md:3` "The third verb's procedure. Unlike `audit` and `author` ..." | third verb | discovery phase | **P** | MISLEAD |
| B3 | `md-domain/CLAUDE.md:124-126` insight `coverage_is_a_report_only_third_verb`, summary line "a REPORT-ONLY third verb" | third verb | see 1.3 -- the three facts survive, the peer-verb inference does not | **P** (a decision record), but load-bearing: a future agent reads this to decide the question. | MISLEAD |
| B4 | `workflow/coverage-detect.js:5`, `:25` "which is why it is a third verb"; `SKILL.md:294` verb enum `audit \| author \| coverage` | third verb / verb | phase | **C** for `SKILL.md:294` (CLI token + `verb: coverage` at `SKILL.md:271`, asserted at `test_domain_members_resolve.py:65`); **P** for the JS comments. | MISLEAD |
| B5 | lane id `coverage_code_subtree`, verdicts `GAPS-FOUND` / `COVERAGE-ASSESSED`, `workflow/coverage-detect.js`, `scripts/discover_coverage.py`, `references/standards/coverage-standards.md`, `references/lanes/coverage-lane.md` | coverage | (see plan -- recommend KEEP) | **C**, heavy: `test_coverage_workflow_contract.py:32-34,152,269-283`; `test_domain_members_resolve.py:65,253-256`; `docs/reference/first-run-experience.md:209`. Note `test_coverage_workflow_contract.py:69-72` asserts the string `coverage` is ABSENT from `gen_workflow_js.py` -- a rename makes that assertion pass vacuously, i.e. it stops testing anything. | INCONSIST |
| B6 | `SKILL.md:54-58` menu entry "check `<directory>` for coverage ... Coverage analysis" | coverage analysis | (naming is fine; the framing "one of the analyses" is the issue) | **C** -- `SKILL.md:92-93` makes these labels the canonical analysis names the run MUST echo. | INCONSIST |

### Group C -- "audit" used where nothing renders a verdict (the roster)

The owner's fixed point: a roster renders no verdict, so it is not an audit.
`SKILL.md:87` already says exactly that. These rows contradict it inside the same
tree.

| # | path:line | current term | framework term | blast radius | sev |
|---|---|---|---|---|---|
| C1 | `SKILL.md:165` -- `audit_skill`'s `invocation_phrasings` includes `"inventory the skills / skill roster / skill hierarchy"` | an inventory request routes into a lane whose verb is `audit` | not an audit; if it emits a file, it is GENERATION | **C-ish** -- a required lane-record field; `test_domain_members_resolve.py:194-205` checks presence and count (>= 3), not content, so removing this one phrasing leaves 5 and passes. | MISLEAD -- directly contradicts `SKILL.md:87` five dozen lines earlier. |
| C2 | `SKILL.md:301-302` "`audit skill` also accepts `roster` / `hierarchy` (with an optional output path or `-` for stdout) for corpus inventory" | invoked as `audit` | not an audit | **C** -- CLI grammar. The optional output path is the tell: with a path it WRITES A DOCUMENT, which under the framework is generation. | MISLEAD |
| C3 | `SKILL.md:103` "A dispatch that runs two (an audit plus its roster inventory) announces both" | presupposes an audit dispatch also runs a roster | two different things, one of which is not an audit | **P** | MISLEAD |
| C4 | `references/audit-framework.md:29-30` "Zero+ **supporting procedures** -- inventory or report procedures over the shared subject (e.g. skill-audit's `roster` and `hierarchy`)"; `:140` same | inventory framed as a procedure of an audit-skill | not an audit | **P inside a CROSS-PLUGIN API FILE.** The file is consumed BY PATH from `plugins/awesome-kit/skills/plugin-ecosystem/SKILL.md:14` and `plugins/prototypes/skills/claude-explorer/SKILL.md:27,107-108,160` (and declared in both plugins' `bootstrap.json:2`). Prose edits are safe; the PATH must not move. | MISLEAD |
| C5 | `SKILL.md:463-465` index entry naming `report-usage.md` (roster/hierarchy CLI) under the skill-domain cluster | -- | -- | **P** | INCONSIST |
| C6 | `references/audit-framework.md:171-186` "Beyond audits: viewer scaffolding ... produces a representation ... a generator script" | viewer framed as a sibling family of audit | under the framework a viewer IS generation (it creates a document that does not exist) | **P**, cross-plugin file (see C4). Consumers `awesome-kit:plugin-ecosystem` and `prototypes:claude-explorer` both self-describe as viewer-kinds. | INCONSIST -- the framework would simplify this section, not correct an error. |

### Group D -- code review as a subtype of audit

| # | path:line | current term | framework term | blast radius | sev |
|---|---|---|---|---|---|
| D1 | `plugins/git-kit/skills/git-code-review/SKILL.md:6,11,17`; `plugins/p4-kit/skills/p4-code-review/SKILL.md` head | "code review" | consistent -- a code review is an audit of a diff, and "code review" is the subtype's name | none | none. **Do not rename.** See 4.3. |
| D2 | `git-code-review/SKILL.md:23-24` covers "CLAUDE.md compliance **audits** in a git repo", "bug **audits** scoped to introduced code"; `p4-code-review/SKILL.md:21-22` same | audit used inside a review skill | consistent under the framework (review is an audit) | none | none -- these rows were latent inconsistencies BEFORE the framework and are resolved BY it. Worth noting as evidence the framework fits the code-review side already. |
| D3 | `git-code-review/SKILL.md:380,382`; `p4:394,396` "this **audits** .md file changes ... this **reviews** the code changes and **audits** the .md changes" | audit vs review split by content type | under the framework both are audits; the split is by SUBJECT (document vs diff), not by kind | **P** (user-facing narration strings) | INCONSIST -- reads as two kinds of activity when it is one kind over two subjects. |
| D4 | ledger wire value `"kind": "md_audit"` -- `plugins/bootstrap/bootstrap_lib/code_review/ledger.py:114,121,124,150,175`; `git SKILL.md:260,263`; `p4 SKILL.md:268,271` | `md_audit` | correct as an AUDIT label, but the token echoes the DELETED `/md-audit` skill | **C -- cross-plugin wire API.** Persisted in on-disk ledgers on consumer machines. A rename orphans every existing ledger entry (silently un-collapsing previously declined findings). | INCONSIST. **Do not rename.** See 4.3. |
| D5 | stale `/md-audit` prose: `ledger.py:5,21,44,45,149,235`; `pipeline.py:55,132,415,423`; `triviality.py:4`; `tests/skills-kit/golden_corpus/expected-lanes/project-doc-lanes.json:12-13` (a recorded decline message telling the reader to re-run under `/md-audit claude-md`) | `/md-audit`, a skill deleted in the 2026-07-29 fold | `/md-domain audit ...` | **P** in the libs; the golden file is a RECORDED EXPECTATION, read by whoever next re-records goldens. | MISLEAD -- names a skill that does not exist. Already owned by plan item `inherited-stale-md-audit-prose`; listed here for completeness, not re-owned. |

### Group E -- the "generated" collision (see 3.2)

| # | path:line | current term | issue | blast radius | sev |
|---|---|---|---|---|---|
| E1 | `references/cohesion-principles.md:467-479` per-artifact role `generated_artifact`: "changes when REGENERATED -- the change driver is the generator plus its inputs, never a hand edit" | "generation" / "regeneration" ALREADY have a meaning in md-domain's own placement spine, and it is a different one | -- | **C** -- a per-artifact role id in the placement spine that every lane defers to (`SKILL.md:392`). | MISLEAD if `author` -> `generate` lands without disambiguation. |
| E2 | `git SKILL.md:223-235`, `:310-314`; `p4 SKILL.md:292,319`; `bootstrap_lib/code_review/generated.py`, `generated_paths.py`; bundle fields `generated_files`, `generated_axis`, `generated_signature`; flag `--review-generated` | "generated" == machine-emitted, NOT reviewed, "review of generated output belongs on the GENERATOR" (`git SKILL.md:231`) | same word, opposite disposition to a document md-domain would "generate" | **C** -- JSON bundle fields + a CLI flag, cross-plugin. | MISLEAD if `author` -> `generate` lands without disambiguation. |

**Counts.** 27 rows: 9 MISLEAD (B1, B2, B3, B4, C1, C2, C3, C4, D5), 2 conditionally
MISLEAD (E1, E2 -- only if the `generate` rename lands), 14 INCONSIST, 2 no-change
(D1, D2).

---

## 3. What the framework does not yet cover

Three genuine gaps. Each is a place to extend the framework, not a place the code is
wrong.

### 3.1 There is no term for a phase that discovers but does not write

Coverage reads code, applies admission criteria, and emits a candidate list with
destinations -- and then stops by contract (`coverage-lane.md:70,189`;
`coverage-standards.md:231-232`). Under the framework it is not audit (it renders no
compliance verdict; `coverage-lane.md:196-199` says so explicitly and forbids
COMPLIANT/NON-COMPLIANT), and it is not generation (it creates no document).

The framework needs one of:

- **(a) Generation is a pipeline with named phases**, of which discovery is one, and
  a phase may be invoked standalone as a dry run. This matches the owner's phrasing
  ("code introspection that leads to fact discovery and documentation belongs in
  generation/regeneration") most closely, and it is cheap: nothing in coverage
  changes.
- **(b) A third top-level term** (discovery / analysis) that feeds both generation and
  regeneration.

**(a) is recommended**, with one caveat that must be written down rather than
inferred: **re-homing coverage under generation must not be allowed to imply it may
write.** Today "report-only" is carried partly by the verb being separate. Under (a),
the report-only contract has to be restated as a property of the standalone entry
point (`coverage-lane.md:40` "remediate workflow | NONE -- report-only, deliberately"
and `test_domain_members_resolve.py:253-256` already pin it mechanically -- so the
mechanism survives; it is the PROSE that would start implying otherwise).

The owner has not said the report-only contract changes. This plan assumes it does
not, and section 4 sequences the framing change so nothing about report-only moves.

### 3.2 "Generation" is already occupied, in the opposite sense

The framework's word for md-domain's producing verb is already in service across
these very plugins meaning *machine-emitted, excluded from review*:

- `cohesion-principles.md:467-470` -- the `generated_artifact` role: "not
  hand-maintained knowledge; placement/maturation rules for authored docs do not
  apply", "nothing may depend on a generated artifact as a load-bearing knowledge
  surface".
- `git SKILL.md:231` -- "review of generated output belongs on the GENERATOR".
- The bundle contract: `generated_files` / `generated_axis` / `generated_signature`,
  `--review-generated` (`git SKILL.md:84,223-235`; `bootstrap_lib/code_review/generated.py`).

A CLAUDE.md md-domain produces is the exact opposite of that: hand-maintained
knowledge, load-bearing, and reviewed. If `author` becomes `generate`, the sentence
"md-domain generated this CLAUDE.md" becomes ambiguous between "md-domain wrote it to
standards" and "it is a generated artifact, do not review it" -- inside two plugins
whose review pipeline branches on precisely that distinction.

**The framework needs a disambiguator.** Options, in order of my preference:

1. Keep the verb token `author` and state in prose that authoring IS generation (and
   regeneration when the target exists). Zero contract churn; the framework is stated
   where it matters and the collision never occurs.
2. Rename the verb `generate` and simultaneously rename the artifact role to
   `machine_generated_artifact` / the bundle axis vocabulary to `machine-emitted`.
   That is a coordinated three-plugin contract change (cohesion-principles role id +
   bundle field names + a CLI flag) for a naming gain.
3. Rename the verb and leave the collision. Not recommended; it manufactures the
   MISLEAD rows E1/E2.

### 3.3 An inventory that writes a file has no home

`SKILL.md:301-302` -- `roster` / `hierarchy` accept "an optional output path or `-`
for stdout". With a path, the roster CREATES A DOCUMENT THAT DOES NOT EXIST, which is
generation by the framework's own definition. Without a path it is a stdout render,
which is neither audit nor generation.

So "a roster is not an audit" (the owner's fixed point) is right, but the framework
does not then say what it IS. The cheapest consistent answer: a roster written to a
path is generation of a generated artifact (and `cohesion-principles.md:467-479`
already has the role for it -- provenance-only, name the generator). A roster to
stdout is a render, and the framework may simply decline to name it.

---

## 4. Remediation plan

Sequenced. Each stage is independently shippable; nothing in stage 1 depends on a
decision in stage 2.

### Stage 0 -- record the framework itself (do first, blocks nothing)

The framework currently exists only in a chat message. Nothing in the three plugins
states it. Every row below is unreviewable until it does.

- Add one `dec_20_*` record to
  `plugins/skills-kit/skills/md-domain/references/provenance/skill-authoring-decisions.md`
  stating the four definitions and the two fixed points, in the file's
  surface / finding / follow-up convention. That file is the declared home for
  framework-vocabulary decisions -- `references/provenance/standards-decisions.md:18`
  explicitly EXCLUDES them and points here.
- Cost: one insight record. No contract impact. No version bump strictly required,
  but it ships inside the plugin, so it rides the next skills-kit bump.

### Stage 1 -- prose-only renames (cheap, no contract impact)

All of these are safe to land in one commit with a skills-kit patch bump. None
touches a lane id, verdict, rule id, taxonomy letter, schema field, CLI token, test
assertion, or cross-plugin path.

1. **Coverage's framing** (rows B1, B2, B3, B4-prose). Replace "third verb" with
   "the discovery phase that feeds generation and regeneration", in `SKILL.md:14,24`,
   `coverage-lane.md:3`, `coverage-detect.js:5,25`. In the same edit, restate the
   report-only contract as an explicit property rather than an inference (per 3.1):
   one sentence at `coverage-lane.md:3-10`.
2. **Amend, do not delete, `coverage_is_a_report_only_third_verb`**
   (`md-domain/CLAUDE.md:124-194`). Keep the three contract facts verbatim -- the
   framework does not touch them (1.3). Rewrite only the `summary` at `:126` and add
   a paragraph recording that the peer-verb inference was superseded on 2026-08-09
   and why. Rename the insight id only if the owner wants it; the id is referenced
   from `dev/tasks/.../plan.md` and this audit, not from shipped code.
3. **The roster contradictions** (rows C1, C2, C3, C4). Remove the roster phrasing at
   `SKILL.md:165` (5 phrasings remain, floor is 3 --
   `test_domain_members_resolve.py:202` passes). Reword `SKILL.md:103` and
   `SKILL.md:301-302` so the roster is described as an inventory a dispatch may also
   run, explicitly NOT an audit, cross-referencing `SKILL.md:87`. Reword
   `audit-framework.md:29-30,140` to say supporting procedures are not audits and
   render no verdict. **Do not move or rename `audit-framework.{md,yaml}`** (4.3).
4. **Name generation and regeneration where the lane already does both** (rows A10,
   A11). In `authoring-lane.md:1-4,29,99`, say plainly: this lane performs GENERATION
   when the artifact does not exist and REGENERATION when it does, and the
   preservation guard is the existing loss-free-deletion rule at `:155-156`. This is
   the single highest-value prose edit in the whole plan -- it makes the framework
   visible at the exact place it operates, at zero risk.
5. **The narration strings** (row D3). `git SKILL.md:380,382`, `p4 SKILL.md:394,396`:
   reword so the split reads as one activity over two subjects. Requires a git-kit
   and p4-kit patch bump each. Low value; bundle it with the next change to those
   files rather than shipping alone.
6. **The stale `/md-audit` prose** (row D5). Already owned by plan item
   `inherited-stale-md-audit-prose`; do it there, not here. Flagged only so the two
   plans do not both claim it.

### Stage 2 -- contract renames (each needs its own decision)

Do not bundle these. Each is a `contracts_preserved_verbatim_through_the_fold`
event -- `md-domain/CLAUDE.md:117-122`: "a change that alters any of those is not a
refactor. It needs its own decision and its own golden-corpus re-record."

**2a. `author` -> `generate` (verb token + lane ids + lane file).** BLOCKED on the
3.2 disambiguation decision. Full break list:

| Breaks | Where |
|---|---|
| Lane ids | `SKILL.md:124-127,228-269`; `test_domain_members_resolve.py:62-64,144-155,258-263` |
| Dispatch-table row keys | `test_domain_members_resolve.py:162-188` parses `"<verb> x <artifact>"` |
| Bound procedure path | `authoring-lane.md` -> new name; `SKILL.md:232,246,260,451`; `test_domain_members_resolve.py:262` asserts the literal string |
| Generated workflow | `workflow/project-doc-remediate.js:93` -- REGENERATE via `scripts/gen_workflow_js.py:339`, never hand-edit |
| CLI token / user muscle memory | `SKILL.md:9,294`; no alias exists by deliberate decision (`md-domain/CLAUDE.md:46-48`) |
| Cross-plugin | `plugins/prototypes/skills/claude-explorer/SKILL.md:119` -- needs a prototypes version bump in the SAME release |
| Docs | `README.md:19`; `cohesion-principles.md:13-14`; `skill-standards.md:740`; `claude-md-standards.md:624`; `audit-lane.md:535` |
| Goldens | Re-record per `md-domain/CLAUDE.md:117-122`. Note: the corpus is per-ARTIFACT and per-audit-lane, so an author-verb rename plausibly produces a no-op diff -- state that deliberately (the same discipline `md-domain/CLAUDE.md:176-186` applied to coverage), do not skip it silently. |

My recommendation is option 1 in 3.2: **do not do 2a.** State the equivalence in
prose (stage 1 item 4) and keep the token. The framework is a model of what the verbs
MEAN; it does not require the tokens to be its own words, and `author` is an accurate
English name for "generate, preserving existing value where a target exists".

**2b. `coverage` lane id / file names.** NOT RECOMMENDED, and cheap to decline: the
framework's complaint about coverage is its PEER STATUS, which is pure framing (stage
1 item 1) and needs no id change. The word "coverage" is also accurate for what the
criteria measure (`coverage-standards.md:1-3`). If renamed anyway, the break list is
row B5, plus the note that `test_coverage_workflow_contract.py:69-72` degrades to a
vacuous assertion and would need rewriting to keep testing anything.

**2c. `verb: coverage` -> a `phase:` key on the lane record.** Only if the owner wants
the framework expressed as DATA rather than prose. Breaks
`test_domain_members_resolve.py:65,144-155` and the `EXPECTED_LANES` shape. Defer;
stage 1 item 1 delivers most of the value.

### 4.3 Do NOT rename -- with reasons

Rigorous list. Each is a place where the framework's word is arguably better and the
rename is still wrong.

1. **Rule ids** -- `C-*`, `R-*`, `A-*`, `H-*`, `PD-*`, `CD-*`, `DD-*`, `CV-*`,
   `SR-*`, and the per-lane letter taxonomies (including the inconsistent `M_`/`R_`/`S_`
   ancestor-convention ids). Preserved verbatim on purpose
   (`md-domain/CLAUDE.md:105-113`). The framework says nothing about them; they are
   opaque handles, not vocabulary. `CV-*` ("coverage") is the only one the framework
   even touches and it is still opaque.
2. **Verdict vocabulary** -- COMPLIANT / NON-COMPLIANT / DIFF-CLEAN / NOT-AUDITED,
   AUTO / DISCUSS / SPECIAL, GAPS-FOUND / COVERAGE-ASSESSED. Same reason, plus
   NOT-AUDITED is a load-bearing fake-gate guard consumed cross-plugin
   (`git SKILL.md:306`; `p4 SKILL.md:315`; `md-domain-review.md:188-204`) and pinned
   at `test_domain_members_resolve.py:224-227` and
   `test_coverage_workflow_contract.py:76-93`.
3. **`references/audit-framework.md` and `.yaml` -- the PATHS.** Consumed by literal
   path from `awesome-kit` and `prototypes` (`awesome-kit/bootstrap.json:2`;
   `plugin-ecosystem/SKILL.md:14`; `prototypes/bootstrap.json:2`;
   `claude-explorer/SKILL.md:27,107-108,160`), per the
   `audit_framework_paths_are_cross_plugin_api` insight in
   `plugins/skills-kit/CLAUDE.md`. The framework arguably makes "audit-framework" too
   narrow a name (it already hosts viewer-kinds -- row C6). Rename anyway = a breaking
   cross-plugin change for a naming gain. **Edit the prose inside; never move the
   file.**
4. **`"kind": "md_audit"` (ledger wire value).** Row D4. It is persisted state on
   consumer machines; renaming it silently un-collapses every previously declined
   finding, which is a behaviour regression disguised as a rename. It is also CORRECT
   under the framework (an md-domain finding IS an audit finding); only its
   resemblance to the deleted `/md-audit` skill is unfortunate, and that is fixed by
   the prose in D5. Fix the comment at `git SKILL.md:263` / `p4 SKILL.md:271` (which
   already says it is a wire value), not the value.
5. **"code review" as the name of git-kit's and p4-kit's skills.** The framework makes
   review a SUBTYPE of audit -- a subtype keeps its own name. Renaming to "diff audit"
   would be actively harmful: it erases the lexical distinction that currently keeps
   md-domain out of code issues. Which leads to:
6. **THE BOUNDARY HAZARD -- do not let one word cover both.** The brief asked for any
   rename that would erode "md-domain informs code review and must NOT identify code
   issues". Here it is, precisely: today the boundary is carried by two distinct words
   over two distinct subjects -- md-domain AUDITS DOCUMENTS
   (`coverage-lane.md:12-21`, `coverage-standards.md:134-145` CV-8,
   `coverage-detect.js:187` "If you find yourself enumerating what is wrong with the
   code..."), git-kit REVIEWS DIFFS. The framework unifies the KIND (both are audits)
   and it would be a short step to unify the WORD. **Do not.** If both activities are
   called "audit" without qualification, CV-8 loses its lexical anchor and the
   defect-list failure mode -- the exact thing two adversarial reviews already
   rejected (`coverage-lane.md:240-248`) -- becomes a natural reading rather than an
   error. Concrete guard to write into the stage-0 dec record: *the framework unifies
   audit as a KIND; the subject still discriminates. Document audit and code review
   are both audits and remain separately named.*
7. **`generated_artifact` role and the `generated_*` bundle vocabulary** (E1, E2),
   unless 3.2 option 2 is chosen deliberately as a coordinated three-plugin change.
8. **The `--review`, `--density`, `--advanced`, `--diff`, `--json` flag names.** The
   framework says nothing about them and they are user-typed.

---

## 5. Recommended durable home

Per `md-domain/references/cohesion-principles.md` and the placement convention in
`plugins/skills-kit/CLAUDE.md` (`conventions`: "Surface a framework decision as a
lessons-learned entry with surface / finding / follow-up provenance ... Land it in
skills/md-domain/references/provenance/ ... or skills/md-domain/CLAUDE.md"), this
document splits three ways. It should NOT move anywhere as a single file.

1. **The framework itself (section "The framework, as given" + section 4.3 item 6, the
   boundary guard)** -> a new `dec_20_*` record in
   `plugins/skills-kit/skills/md-domain/references/provenance/skill-authoring-decisions.md`.
   That is the declared home: `standards-decisions.md:18` excludes framework-vocabulary
   decisions and names this file. CCP fit: it changes when the framework's definitions
   change, which is the same cadence as the other `dec_N` records.

2. **The consequence for md-domain's own shape (section 1.3 -- coverage is not a peer
   verb; and whichever way 3.2 is decided)** -> an amendment to
   `plugins/skills-kit/skills/md-domain/CLAUDE.md`, specifically the existing
   `coverage_is_a_report_only_third_verb` insight at `:124-194`. That file's declared
   scope is "shape decisions about the md-domain skill itself" (`:19-20`), and the
   verb roster is exactly that. Amend rather than add: two records disagreeing about
   whether coverage is a peer verb is the drift the SSOT rule exists to prevent.

3. **This divergence table and the remediation plan** -> they have NO durable home
   inside `plugins/`, and that is deliberate. Per the root CLAUDE.md's
   `no_build_machinery_in_published_plugins` rule, a table of `path:line` citations
   into our own tree with a per-row blast radius is maintainer material: nobody on a
   machine that is not ours reads it. If it must outlive the task, its home is
   `docs/reference/` at the repo root (which does not ship), as something like
   `docs/reference/md-domain-terminology-alignment.md`. Otherwise it dies with the
   task folder once the two decision records above exist -- which is the correct
   outcome, because the decisions are the durable part and the audit is the working
   note that produced them.

**Explicitly not recommended:** a new reference file under
`md-domain/references/`. It would ship, it serves no consumer, and it duplicates a
decision that belongs in the provenance log.

---

## 6. Critical-infrastructure disclosure

- **Created:** this file only. It was written to
  `dev/tasks/md-domain-review-enablement/terminology-audit.md`, which was then untracked
  scratch under a gitignored `dev/` and the only copy. Both of those facts have since
  changed and the sentence is kept only as provenance: `cc1a5d5d` promoted this document
  to `docs/reference/terminology/terminology-audit.md`, where it is tracked. The task
  folder still exists under its original name; the tracked copy is authoritative.
- **Changed:** nothing. No file under `plugins/`, `tests/`, `scripts/`, or `docs/` was
  edited. No rename was applied.
- **Git:** no branch created, no branch switched, no `git add`, no commit, no push.
  The shared index was not touched.
- **Reads:** read-only throughout. No script was executed against the tree beyond
  `grep` / `sed` / `find`.
