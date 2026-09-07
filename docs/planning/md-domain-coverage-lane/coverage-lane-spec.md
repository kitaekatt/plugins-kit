# Spec: the `coverage` verb (opt-in, report-only)

Status: SPEC, partially blocked. The PIPELINE below is settled and buildable. The
ANALYSIS CRITERIA -- what makes a code-derived fact worth putting in a CLAUDE.md --
are deliberately absent, pending two documents from the owner. Do not invent them.

Replaces the failed "Part 5 hazard sweep".

## What this is for, stated before anything else

md-domain is **not a code-review tool**. It ensures a CLAUDE.md **informs** code
review. Reading code is in scope only as a SOURCE OF INSIGHT for the CLAUDE.md
that will be ambient for that code; it is never a hunt for defects.

This distinction is load-bearing and was got wrong once already (owner correction,
2026-08-08). The consequences, which the rest of this document obeys:

- **Do not spend effort finding code deficiencies.** That is a code review's job,
  conducted AGAINST the CLAUDE.md this verb helps produce. A coverage run that
  returns a defect list has done the wrong work, however accurate the list.
- **A deficiency noticed in passing is reported only if SEVERE**, and the only
  place it is ever written is a CLAUDE.md. There is no code-fix output, no
  make-it-loud output, and no defect ledger.
- The unit of output is therefore always **a CLAUDE.md insight that is missing**,
  never a hazard, a candidate fix, or a finding about the code as such.

The earlier version of this spec inverted this: it routed candidates code-fix ->
make-it-loud -> document, with documentation as the LAST resort. That routing is
retired. It was a reasonable answer to a question this verb is not asking.

## The insight this rests on

md-domain ALREADY knows how to derive CLAUDE.md content from code -- in the
AUTHORING direction. `claude-md-standards.md:385-390` defines the observation
kinds an author is meant to write up, and says the authoring direction asks which
are "present and silent" in a directory. The audit direction is what refuses to
look (`:431-433`, "a validator over existing claims, not a gotcha crawler").

So the gap does not need a new taxonomy of code facts. It needs the existing
authoring observation kinds run over a subtree, in a reporting mode, against the
CLAUDE.md chain that actually loads for that subtree. That is the whole design.

## Subject and unit

**Unit: `(code subtree, its ambient CLAUDE.md chain)`.** Not a markdown file --
this is the first subject in the skill that is code. That is why it cannot be a
criterion inside `audit_claude_md`: the per-file lanes enumerate CLAUDE.md files,
and no criterion can have a subject its lane cannot enumerate. The decisive case
is a subtree with NO CLAUDE.md at all, which is exactly what this verb exists to
catch and exactly what a per-file lane never reaches.

Discovery: the user names a directory, or the verb takes the current diff. There
is NO whole-repo default -- an unbounded default is how this becomes expensive
and non-idempotent.

## Shape: a report-only third verb (settled 2026-08-08)

`coverage` is a third VERB alongside `audit` and `author`, with its own procedure
at `references/lanes/coverage-lane.md`. It is NOT an audit lane and NOT a flag on
one. Three findings settle it, each verified against source:

- `audit-lane.md:19-22` -- everything before the references section "applies to
  the three per-file lanes". `audit_references` is a carved-out outlier, not a
  general extension point.
- `audit-lane.md` (Gotchas, the Idempotency bullet) -- "The same input produces
  the same detected finding set and, in normal mode, the same verdict" is an
  audit INVARIANT. This verb disclaims idempotency (see below), so it cannot be
  an audit lane without breaking a contract the audit family relies on.
- `tests/skills-kit/test_domain_members_resolve.py:213-217` requires every
  `audit_*` lane but references to declare `NOT-AUDITED` + `DIFF-CLEAN`, and
  `:236` requires every `verb == "audit"` record to bind a `workflow_remediate`.
  A third verb satisfies both by not matching them, rather than by exception.

**Report-only** is what keeps the third verb cheap: with no remediation phase
there is no remediate workflow, so the sonnet/low pin does not apply and
`gen_workflow_js.py` -- which assumes per-file edits and `applied/skipped/failed`
results -- is not involved at all.

## Intent gating -- stronger than opt-in

Code analysis is materially more expensive than a document audit, so a flag that
merely defaults to off is not sufficient.

1. The verb NEVER runs as part of an `audit` or `author` invocation, and never as
   a side effect of any other lane.
2. It runs only on **expressed user intent** to analyze code for CLAUDE.md
   content.
3. When intent is **ambiguous** -- the request could be read as a document audit,
   or the scope is unstated -- CONFIRM with `AskUserQuestion` before running.
   Confirm the cost and the scope in the same question. Do not infer intent from
   the presence of code in the named directory.

Point 3 is a requirement of the procedure, not a courtesy. The failure it
prevents is a user asking for a docs check and receiving an expensive code
analysis they did not ask for.

## Verdict vocabulary -- distinct, never mixed with document compliance

`GAPS-FOUND` | `COVERAGE-ASSESSED`

The verb **never** emits COMPLIANT / NON-COMPLIANT and **never** alters a
document verdict. A file can be COMPLIANT and its subtree GAPS-FOUND at the same
time; those answer different questions, and conflating them is the exact misread
that started this investigation.

## Output: what a candidate is

A candidate is **a fact about the code that belongs in a CLAUDE.md and is not
ambient for the code it describes**. Each carries the destination file the
placement algorithm selects -- ambient for the code it describes, per
`references/cohesion-principles.md`, never wherever is convenient.

Reporting a candidate is not a commitment to write it. Nothing is auto-applied.

**The severe-deficiency carve-out.** If the analysis incidentally establishes that
code is defective, that is reportable ONLY when severe, and only as CLAUDE.md
content. The bar is deliberately high, and two prior objections survive as the
reason:

- Documenting a hazard can FOSSILIZE a bug -- writing "this silently truncates at
  65536" into ambient prose enshrines as behaviour-to-preserve something whose
  right answer was a code change.
- A stated invariant that the code contradicts is a contradiction to surface, not
  to write down twice.

When in doubt, do not report it. A missed deficiency is recoverable by a code
review; a fossilized one is not.

## Exclusions

**Structural exclusions, checked before any code is read.** Skip these and SAY
in the report that they were skipped:

- vendored / third-party trees (`node_modules/`, `vendor/`, `third_party/`,
  `Pods/`, `target/`, `dist/`, `build/`)
- generated trees and files (an existing modality covers generated content,
  `claude-md-standards.md:413`)
- symlinked trees that resolve outside the subtree
- nested repositories and submodules -- the ancestor walk stops at `.git`, so a
  nested repo's ambient chain is NOT the outer repo's

**Existing-coverage suppression, applied during assessment.** A fact already
carried by an ambient claim that RESOLVES is not a candidate. This cannot be a
pre-read exclusion: establishing it requires reading the ambient document and
usually the source it anchors to. (An earlier revision listed this among the
pre-read exclusions, which was not implementable.)

## Cost and idempotency bounds

- Bounded by the named subtree or diff, never the repo.
- A candidate ceiling PER SUBTREE (not per run; a per-run cap divided across
  subjects gives each an arbitrary share). When it is hit, SAY SO rather than
  silently truncating. Silent truncation in the tool that reports silent
  truncation would be its own joke, and the repo's own rule requires a capped run
  to announce the cap.
- Idempotency is NOT claimed. The verb is advisory and JUDGMENT-severity
  throughout. Claiming determinism for LLM candidate selection is the specific
  overreach that failed review; the honest posture is "advisory, re-runs may
  differ, nothing auto-applies".

## What it deliberately does NOT do

- Does not run by default, and not without expressed intent (see Intent gating).
- Does not auto-apply anything.
- Does not add content to any CLAUDE.md by itself.
- Does not emit code fixes, tests, or defect reports.
- Does not claim to be exhaustive. Two thorough reviewers sampled the same corpus
  and found largely DIFFERENT hazards; a report from this verb is a sample, and
  the report must say so.

## Validation gate before it ships

Run the four negative controls in `coverage-gap.md` ("The negative controls a
coverage test needs"), especially the documented-loud-and-test-enforced near
miss, and measure precision on whatever corpus is at hand that the design was not
built against.

If no such corpus exists: SHIP and record the limitation. The blocking
language-family requirement an earlier revision carried here was retired
2026-08-08 as an accepted limitation -- see `coverage-gap.md`, "Scope of the
rule", which is authoritative over this paragraph.

Recall on the flecs answer key is the WEAKER measure and must not be reported
alone -- the key derived the design, so recall is near-1 by construction.
